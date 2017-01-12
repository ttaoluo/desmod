"""Simulation model with batteries included."""

from __future__ import division
from contextlib import contextmanager, closing
from multiprocessing import cpu_count, Process, Queue
from threading import Thread
import os
import random
import shutil
import timeit

import simpy
import six
import yaml

from desmod.config import factorial_config
from desmod.timescale import parse_time, scale_time
from desmod.tracer import TraceManager


class SimEnvironment(simpy.Environment):
    """Simulation Environment.

    The :class:`SimEnvironment` class is a :class:`simpy.Environment` subclass
    that adds some useful features:

     - Access to the configuration dictionary (`config`).
     - Access to a seeded pseudo-random number generator (`rand`).
     - Access to the simulation timescale (`timescale`).
     - Access to the simulation duration (`duration`).

    Some models may need to share additional state with all its
    :class:`desmod.component.Component` instances. SimEnvironment may be
    subclassed to add additional members to achieve this sharing.

    :param dict config: A fully-initialized configuration dictionary.

    """
    def __init__(self, config):
        super(SimEnvironment, self).__init__()
        #: The configuration dictionary.
        self.config = config

        #: The pseudo-random number generator; an instance of
        #: :class:`random.Random`.
        self.rand = random.Random()
        seed = config.setdefault('sim.seed', None)
        if six.PY3:
            self.rand.seed(seed, version=1)
        else:
            self.rand.seed(seed)

        timescale_str = self.config.setdefault('sim.timescale', '1 s')

        #: Simulation timescale ``(magnitude, units)`` tuple. The current
        #: simulation time is ``now * timescale``.
        self.timescale = parse_time(timescale_str)

        duration = config.setdefault('sim.duration', '0 s')

        #: The intended simulation duration, in units of :attr:`timescale`.
        self.duration = scale_time(parse_time(duration), self.timescale)

        #: The simulation runs "until" this event. By default, this is the
        #: configured "sim.duration", but may be overridden by subclasses.
        self.until = self.duration

        #: :class:`TraceManager` instance.
        self.tracemgr = TraceManager(self)

    def time(self, t=None, unit='s'):
        """The current simulation time scaled to specified unit.

        :param float t: Time in simulation units. Default is :attr:`now`.
        :param str unit: Unit of time to scale to. Default is 's' (seconds).
        :returns: Simulation time scaled to to `unit`.

        """
        target_scale = parse_time(unit)
        ts_mag, ts_unit = self.timescale
        sim_time = ((self.now if t is None else t) * ts_mag, ts_unit)
        return scale_time(sim_time, target_scale)


class SimStopEvent(simpy.Event):
    """Event appropriate for stopping the simulation.

    An instance of this event may be used to override `SimEnvironment.until` to
    dynamically choose when to stop the simulation. The simulation may be
    stopped by calling :meth:`schedule()`. The optional `delay` parameter may
    be used to schedule the simulation to stop at an offset from the current
    simulation time.

    """

    def schedule(self, delay=0):
        assert not self.triggered
        assert delay >= 0
        self._ok = True
        self._value = None
        self.env.schedule(self, simpy.events.URGENT, delay)


class _Workspace(object):
    """Context manager for workspace directory management."""
    def __init__(self, config):
        self.workspace = config.setdefault('sim.workspace', os.curdir)
        self.overwrite = config.setdefault('sim.workspace.overwrite', False)
        self.prev_dir = os.getcwd()

    def __enter__(self):
        if os.path.relpath(self.workspace) != os.curdir:
            workspace_exists = os.path.isdir(self.workspace)
            if self.overwrite and workspace_exists:
                shutil.rmtree(self.workspace)
            if self.overwrite or not workspace_exists:
                os.makedirs(self.workspace)
            os.chdir(self.workspace)

    def __exit__(self, *exc):
        os.chdir(self.prev_dir)


def simulate(config, top_type, env_type=SimEnvironment, reraise=True,
             progress_queue=None):
    """Initialize, elaborate, and run a simulation.

     All exceptions are caught by `simulate()` so they can be logged and
     captured in the result file. By default, any unhandled exception caught by
     `simulate()` will be re-raised. Setting `reraise` to False prevents
     exceptions from propagating to the caller. Instead, the returned result
     dict will indicate if an exception occurred via the 'sim.exception' item.

    :param dict config: Configuration dictionary for the simulation.
    :param top_type: The model's top-level Component subclass.
    :param env_type: :class:`SimEnvironment` subclass.
    :param bool reraise: Should unhandled exceptions propogate to the caller.
    :returns:
        Dictionary containing the model-specific results of the simulation.
    """
    t0 = timeit.default_timer()
    result = {}
    try:
        with _Workspace(config):
            env = env_type(config)
            with closing(env.tracemgr):
                try:
                    top_type.pre_init(env)
                    env.tracemgr.flush()
                    with _progress_notification(env, progress_queue):
                        top = top_type(parent=None, env=env)
                        top.elaborate()
                        env.tracemgr.flush()
                        env.run(until=env.until)
                        env.tracemgr.flush()
                        top.post_simulate()
                        env.tracemgr.flush()
                        top.get_result(result)
                except BaseException as e:
                    env.tracemgr.trace_exception()
                    result['sim.exception'] = repr(e)
                    raise
                else:
                    result['sim.exception'] = None
                finally:
                    env.tracemgr.flush()
                    result['config'] = config
                    result['sim.now'] = env.now
                    result['sim.time'] = env.time()
                    result['sim.runtime'] = timeit.default_timer() - t0
                    _dump_result(config.setdefault('sim.result.file'), result)
    except BaseException as e:
        if reraise:
            raise
        result.setdefault('config', config)
        result.setdefault('sim.runtime', timeit.default_timer() - t0)
        if result.get('sim.exception') is None:
            result['sim.exception'] = repr(e)
    return result


def simulate_factors(base_config, factors, top_type,
                     env_type=SimEnvironment, jobs=None):
    """Run multi-factor simulations in separate processes.

    The `factors` are used to compose specialized config dictionaries for the
    simulations.

    The :mod:`python:multiprocessing` module is used run each simulation with a
    separate Python process. This allows multi-factor simulations to run in
    parallel on all available CPU cores.

    :param dict base_config: Base configuration dictionary to be specialized.
    :param list factors: List of factors.
    :param top_type: The model's top-level Component subclass.
    :param env_type: :class:`SimEnvironment` subclass.
    :param int jobs: User specified number of concurent processes.
    :returns: Sequence of result dictionaries for each simulation.

    """
    configs = list(factorial_config(base_config, factors, 'sim.special'))
    base_workspace = base_config.setdefault('sim.workspace', os.curdir)
    overwrite = base_config.setdefault('sim.workspace.overwrite', False)
    for seq, config in enumerate(configs):
        config['sim.workspace'] = os.path.join(base_workspace, str(seq))
    if (overwrite and
            os.path.relpath(base_workspace) != os.curdir and
            os.path.isdir(base_workspace)):
        shutil.rmtree(base_workspace)
    return simulate_many(configs, top_type, env_type, jobs)


def simulate_many(configs, top_type, env_type=SimEnvironment, jobs=None):
    """Run multiple experiments in separate processes.

    The :mod:`python:multiprocessing` module is used run each simulation with a
    separate Python process. This allows multi-factor simulations to run in
    parallel on all available CPU cores.

    :param dict configs: list of configuration dictionary for the simulation.
    :param top_type: The model's top-level Component subclass.
    :param env_type: :class:`SimEnvironment` subclass.
    :param int jobs: User specified number of concurent processes.
    :returns: Sequence of result dictionaries for each simulation.

    """
    progress_enable = any(config.setdefault('sim.progress.enable', False)
                          for config in configs)

    progress_queue = Queue() if progress_enable else None
    result_queue = Queue()
    config_queue = Queue()

    for seq, config in enumerate(configs):
        config['sim.seq'] = seq
        config['sim.progress.enable'] = progress_enable
        config_queue.put(config)

    num_workers = min(len(configs), cpu_count())
    if jobs is not None:
        num_workers = min(num_workers, jobs)

    for i in range(num_workers):
        worker = Process(name='sim-worker-{}'.format(i),
                         target=_simulate_worker,
                         args=(top_type, env_type, False, progress_queue,
                               config_queue, result_queue))
        worker.daemon = True    # Workers die if main process dies.
        worker.start()
        config_queue.put(None)  # A stop sentinel for each worker.

    if progress_enable:
        progress_thread = Thread(target=_consume_progress,
                                 args=(configs, progress_queue))
        progress_thread.daemon = True
        progress_thread.start()

    results = [result_queue.get() for _ in configs]
    return sorted(results, key=lambda r: r['config']['sim.seq'])


def _simulate_worker(top_type, env_type, reraise, progress_queue, config_queue,
                     result_queue):
    while True:
        config = config_queue.get()
        if config is None:
            break
        result = simulate(config, top_type, env_type, reraise, progress_queue)
        result_queue.put(result)


def _dump_result(filename, result):
    if filename is not None:
        with open(filename, 'w') as result_file:
            yaml.safe_dump(result, stream=result_file)


def _get_progressbar(config):
    import progressbar

    pbar = progressbar.ProgressBar(min_value=0, max_value=1,
                                   widgets=[progressbar.Percentage(),
                                            progressbar.Bar(),
                                            progressbar.ETA()])

    max_width = config.setdefault('sim.progress.max_width')
    if max_width and pbar.term_width > max_width:
        pbar.term_width = max_width

    return pbar


@contextmanager
def _progress_notification(env, progress_queue):
    if env.config.setdefault('sim.progress.enable', False):
        interval = env.duration / 100
        seq = env.config.get('sim.seq')

        if seq is None:
            pbar = _get_progressbar(env.config)

            def progress():
                while True:
                    pbar.update(env.now / env.duration)
                    yield env.timeout(interval)

            env.process(progress())

            try:
                yield None
            finally:
                pbar.finish()
        else:
            def progress():
                while True:
                    progress_queue.put((seq, env.now / env.duration))
                    yield env.timeout(interval)

            env.process(progress())

            try:
                yield None
            finally:
                progress_queue.put((seq, 1))
    else:
        yield None


def _consume_progress(configs, progress_queue):
    pbar = _get_progressbar(configs[0])
    notifiers = {config['sim.seq']: 0 for config in configs}
    total_progress = 0

    try:
        while total_progress < 1:
            seq, progress = progress_queue.get()
            notifiers[seq] = progress
            total_progress = sum(notifiers.values()) / len(notifiers)
            pbar.update(total_progress)
    except KeyboardInterrupt:
        pass
    else:
        pbar.finish()
