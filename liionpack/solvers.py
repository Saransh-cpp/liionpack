#
# Solvers
#
import liionpack as lp
from liionpack.solver_utils import _create_casadi_objects as cco
from liionpack.solver_utils import _serial_step as ss
from liionpack.solver_utils import _mapped_step as ms
import ray
import numpy as np
import time as ticker
from dask.distributed import Client
from tqdm import tqdm


class generic_actor:
    def __init__(self):
        pass

    def setup(
        self,
        Nspm,
        sim_func,
        parameter_values,
        dt,
        I_init,
        htc_init,
        variable_names,
        initial_soc,
        nproc,
    ):
        # Casadi specific arguments
        if nproc > 1:
            mapped = True
        else:
            mapped = False
        # Solver set up
        self.step_solutions = [None] * Nspm
        # Set up simulation
        self.parameter_values = parameter_values
        if sim_func is None:
            self.simulation = lp.create_simulation(
                self.parameter_values, make_inputs=True
            )
        else:
            self.simulation = sim_func(parameter_values)
        lp.update_init_conc(self.simulation, SoC=initial_soc)
        # Set up integrator
        casadi_objs = cco(
            I_init, htc_init, self.simulation, dt, Nspm, nproc, variable_names, mapped
        )
        self.integrator = casadi_objs["integrator"]
        self.variables_fn = casadi_objs["variables_fn"]
        self.t_eval = casadi_objs["t_eval"]
        self.event_names = casadi_objs["event_names"]
        self.events_fn = casadi_objs["events_fn"]
        self.last_events = None
        self.event_change = None
        if mapped:
            self.step_fn = ms
        else:
            self.step_fn = ss

    def step(self, inputs):
        # Solver Step
        self.step_solutions, self.var_eval, self.events_eval = self.step_fn(
            self.simulation.built_model,
            self.step_solutions,
            inputs,
            self.integrator,
            self.variables_fn,
            self.t_eval,
            self.events_fn,
        )
        return self.check_events()

    def check_events(self):
        if self.last_events is not None:
            # Compare changes
            new_sign = np.sign(self.events_eval)
            old_sign = np.sign(self.last_events)
            self.event_change = (old_sign * new_sign) < 0
            self.last_events = self.events_eval
            return np.any(self.event_change)
        else:
            self.last_events = self.events_eval
            return False

    def get_event_change(self):
        return self.event_change

    def get_event_names(self):
        return self.event_names

    def output(self):
        return self.var_eval


@ray.remote(num_cpus=1)
class ray_actor(generic_actor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class generic_manager:
    def __init__(
        self,
    ):
        pass

    def solve(
        self,
        netlist,
        sim_func,
        parameter_values,
        experiment,
        htc,
        output_variables,
        initial_soc,
        nproc,
    ):
        self.netlist = netlist
        self.sim_func = sim_func
        self.parameter_values = parameter_values
        # Get netlist indices for resistors, voltage sources, current sources
        Ri_map = netlist["desc"].str.find("Ri") > -1
        V_map = netlist["desc"].str.find("V") > -1
        I_map = netlist["desc"].str.find("I") > -1
        Terminal_Node = np.array(netlist[I_map].node1)
        Nspm = np.sum(V_map)

        self.split_models(Nspm, nproc, htc)

        # Generate the protocol from the supplied experiment
        protocol = lp.generate_protocol_from_experiment(experiment)
        self.dt = experiment.period
        Nsteps = len(protocol)

        # Solve the circuit to initialise the electrochemical models
        V_node, I_batt = lp.solve_circuit_vectorized(netlist)

        # The simulation output variables calculated at each step for each battery
        # Must be a 0D variable i.e. battery wide volume average - or X-averaged for
        # 1D model
        self.variable_names = [
            "Terminal voltage [V]",
            "Measured battery open circuit voltage [V]",
        ]
        if output_variables is not None:
            for out in output_variables:
                if out not in self.variable_names:
                    self.variable_names.append(out)
            # variable_names = variable_names + output_variables
        Nvar = len(self.variable_names)

        # Storage variables for simulation data
        self.shm_i_app = np.zeros([Nsteps, Nspm], dtype=float)
        self.output = np.zeros([Nvar, Nsteps, Nspm], dtype=float)

        # Initialize currents in battery models
        self.shm_i_app[0, :] = I_batt * -1

        # Step forward in time
        time = 0
        self.timestep = 0
        end_time = self.dt * Nsteps
        V_terminal = []
        record_times = []

        v_cut_lower = parameter_values["Lower voltage cut-off [V]"]
        v_cut_higher = parameter_values["Upper voltage cut-off [V]"]

        # Solver specific setup
        self.setup_actors(nproc, self.shm_i_app[0, 0], htc[0], initial_soc)

        sim_start_time = ticker.time()
        lp.logger.notice("Starting step solve")
        for step in tqdm(range(Nsteps), desc="Stepping simulation"):
            # Solver specific steps
            self.step_actors()
            self.get_actor_output(step)

            time += self.dt

            # Calculate internal resistance and update netlist
            temp_v = self.output[0, step, :]
            temp_ocv = self.output[1, step, :]
            # This could be used instead of Equivalent ECM resistance which has
            # been changing definition
            temp_Ri = (temp_ocv - temp_v) / self.shm_i_app[step, :]
            # Make Ri more stable
            current_cutoff = np.abs(self.shm_i_app[step, :]) < 1e-6
            temp_Ri[current_cutoff] = 1e-12

            netlist.loc[V_map, ("value")] = temp_ocv
            netlist.loc[Ri_map, ("value")] = temp_Ri
            netlist.loc[I_map, ("value")] = protocol[step]

            if time <= end_time:
                record_times.append(time)
                V_node, I_batt = lp.solve_circuit_vectorized(netlist)
                V_terminal.append(V_node[Terminal_Node][0])
            if time < end_time:
                self.shm_i_app[step + 1, :] = I_batt[:] * -1

            # Stop if voltage limits are reached
            if np.any(temp_v < v_cut_lower):
                print("Low voltage limit reached")
                break
            if np.any(temp_v > v_cut_higher):
                print("High voltage limit reached")
                break

            self.timestep += 1
        lp.logger.notice("Step solve finished")
        self.cleanup()
        # Collect outputs
        self.all_output = {}
        self.all_output["Time [s]"] = np.asarray(record_times)
        self.all_output["Pack current [A]"] = np.asarray(protocol[: step + 1])
        self.all_output["Pack terminal voltage [V]"] = np.asarray(V_terminal)
        self.all_output["Cell current [A]"] = self.shm_i_app[: step + 1, :]
        for j in range(Nvar):
            self.all_output[self.variable_names[j]] = self.output[j, : step + 1, :]

        toc = ticker.time()

        lp.logger.notice(
            "Total stepping time " + str(np.around(toc - sim_start_time, 3)) + "s"
        )
        lp.logger.notice(
            "Time per step " + str(np.around((toc - sim_start_time) / Nsteps, 3)) + "s"
        )
        return self.all_output

    def actor_i_app(self, index):
        actor_indices = self.split_index[index]
        return self.shm_i_app[self.timestep, actor_indices]

    def actor_htc(self, index):
        return self.htc[index]

    def build_inputs(self):
        inputs = []
        for i in range(len(self.actors)):
            I_app = self.actor_i_app(i)
            htc = self.actor_htc(i)
            inputs.append(lp.build_inputs_dict(I_app, htc))
        return inputs

    def split_models(self, Nspm, nproc, htc):
        pass

    def setup_actors(self, nproc, I_init, htc_init, initial_soc):
        pass

    def step_actors(self):
        pass

    def get_actor_output(self, step):
        pass

    def cleanup(self):
        pass


class ray_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        lp.logger.notice("Ray initialization started")
        ray.init()
        lp.logger.notice("Ray initialization complete")

    def split_models(self, Nspm, nproc, htc):
        # Manage the number of SPM models per worker
        self.htc = np.array_split(htc, nproc)
        self.split_index = np.array_split(np.arange(Nspm), nproc)
        self.spm_per_worker = [len(s) for s in self.split_index]

    def setup_actors(self, nproc, I_init, htc_init, initial_soc):
        tic = ticker.time()
        # Ray setup an actor for each worker
        self.actors = []
        for i in range(nproc):
            self.actors.append(lp.ray_actor.remote())
        setup_futures = []
        for i, a in enumerate(self.actors):
            # Create actor on each worker containing a simulation
            setup_futures.append(
                a.setup.remote(
                    Nspm=self.spm_per_worker[i],
                    sim_func=self.sim_func,
                    parameter_values=self.parameter_values,
                    dt=self.dt,
                    I_init=I_init,
                    htc_init=htc_init,
                    variable_names=self.variable_names,
                    initial_soc=initial_soc,
                    nproc=1,
                )
            )
        _ = [ray.get(f) for f in setup_futures]
        toc = ticker.time()
        lp.logger.notice(
            "Ray actors setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        t1 = ticker.time()
        future_steps = []
        inputs = self.build_inputs()
        for i, pa in enumerate(self.actors):
            future_steps.append(pa.step.remote(inputs[i]))
        events = [ray.get(fs) for fs in future_steps]
        if np.any(events):
            lp.logger.warning("Events were triggered")
        t2 = ticker.time()
        lp.logger.info("Ray actors stepped in " + str(np.around(t2 - t1, 3)) + "s")

    def get_actor_output(self, step):
        t1 = ticker.time()
        futures = []
        for actor in self.actors:
            futures.append(actor.output.remote())
        for i, f in enumerate(futures):
            out = ray.get(f)
            self.output[:, step, self.split_index[i]] = out
        t2 = ticker.time()
        lp.logger.info(
            "Ray actor output retrieved in " + str(np.around(t2 - t1, 3)) + "s"
        )

    def cleanup(self):
        for actor in self.actors:
            ray.kill(actor)
        lp.logger.notice("Shutting down Ray")
        ray.shutdown()


class casadi_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def split_models(self, Nspm, nproc, htc):
        # For casadi there is no need to split the models as we pass them all
        # to the integrator however we still want the global variables to be
        # used in the same generic way
        self.spm_per_worker = Nspm
        self.split_index = np.array_split(np.arange(Nspm), 1)
        self.htc = np.array_split(htc, 1)

    def setup_actors(self, nproc, I_init, htc_init, initial_soc):
        # For casadi we do not use multiple actors but instead the integrator
        # function that is generated by casadi handles multithreading behind
        # the scenes
        tic = ticker.time()

        self.actors = [generic_actor()]
        for a in self.actors:
            a.setup(
                Nspm=self.spm_per_worker,
                sim_func=self.sim_func,
                parameter_values=self.parameter_values,
                dt=self.dt,
                I_init=I_init,
                htc_init=htc_init,
                variable_names=self.variable_names,
                initial_soc=initial_soc,
                nproc=nproc,
            )
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        tic = ticker.time()
        events = self.actors[0].step(self.build_inputs()[0])
        if events:
            lp.logger.warning("Events were triggered")
            event_change = self.actors[0].get_event_change()
            Nr, Nc = event_change.shape
            event_names = self.actors[0].get_event_names()
            for r in range(Nr):
                if np.any(event_change[r, :]):
                    lp.logger.warning(
                        event_names[r]
                        + ", Batteries: "
                        + str(np.where(event_change[r, :])[1].tolist())
                    )
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor stepped in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def get_actor_output(self, step):
        tic = ticker.time()
        self.output[:, step, :] = self.actors[0].output()
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor output got in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def cleanup(self):
        pass


class dask_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def split_models(self, Nspm, nproc, htc):
        # Manage the number of SPM models per worker
        self.htc = np.array_split(htc, nproc)
        self.split_index = np.array_split(np.arange(Nspm), nproc)
        self.spm_per_worker = [len(s) for s in self.split_index]

    def setup_actors(self, nproc, I_init, htc_init, initial_soc):
        # Set up a casadi actor on each process
        lp.logger.notice("Dask initialization started")
        self.client = Client(n_workers=nproc)
        lp.logger.notice("Dask initialization complete")
        tic = ticker.time()
        futures = []
        for i in range(nproc):
            # Create actor on each worker containing a simulation
            futures.append(self.client.submit(generic_actor, actor=True, pure=False))
        self.actors = [af.result() for af in futures]
        futures = []
        for i, a in enumerate(self.actors):
            futures.append(
                a.setup(
                    Nspm=self.spm_per_worker[i],
                    sim_func=self.sim_func,
                    parameter_values=self.parameter_values,
                    I_init=I_init,
                    htc_init=htc_init,
                    dt=self.dt,
                    variable_names=self.variable_names,
                    initial_soc=initial_soc,
                    nproc=1,
                )
            )

        _ = [af.result() for af in futures]
        toc = ticker.time()
        lp.logger.info(
            "Dask actors setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        tic = ticker.time()
        inputs = self.build_inputs()
        future_steps = []
        for i, a in enumerate(self.actors):
            future_steps.append(a.step(inputs=inputs[i]))
        events = [af.result() for af in future_steps]
        if np.any(events):
            lp.logger.warning("Events were triggered")
        toc = ticker.time()
        lp.logger.info(
            "Dask actors stepped in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def get_actor_output(self, step):
        tic = ticker.time()
        future_gets = []
        for i, a in enumerate(self.actors):
            future_gets.append(a.output())
        for i, fg in enumerate(future_gets):
            out = fg.result()
            self.output[:, step, self.split_index[i]] = out
        toc = ticker.time()
        lp.logger.info(
            "Dask,actors output got in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def cleanup(self):
        lp.logger.notice("Shutting down Dask client")
        self.client.shutdown()
