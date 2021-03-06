"""
libuv loop implementation
"""
# pylint: disable=no-member
from __future__ import absolute_import, print_function

import os
from collections import defaultdict
from collections import namedtuple
from operator import delitem
import signal

from gevent._ffi import _dbg # pylint: disable=unused-import
from gevent._ffi.loop import AbstractLoop
from gevent.libuv import _corecffi # pylint:disable=no-name-in-module,import-error
from gevent._ffi.loop import assign_standard_callbacks
from gevent._ffi.loop import AbstractCallbacks

ffi = _corecffi.ffi
libuv = _corecffi.lib

__all__ = [
]


class _Callbacks(AbstractCallbacks):

    def _find_loop_from_c_watcher(self, watcher_ptr):
        loop_handle = ffi.cast('uv_handle_t*', watcher_ptr).data
        return self.from_handle(loop_handle)

    def python_sigchld_callback(self, watcher_ptr, _signum):
        self.from_handle(ffi.cast('uv_handle_t*', watcher_ptr).data)._sigchld_callback()

_callbacks = assign_standard_callbacks(ffi, libuv, _Callbacks,
                                       [('python_sigchld_callback', None)])

from gevent._ffi.loop import EVENTS
GEVENT_CORE_EVENTS = EVENTS # export

from gevent.libuv import watcher as _watchers # pylint:disable=no-name-in-module

_events_to_str = _watchers._events_to_str # export

READ = libuv.UV_READABLE
WRITE = libuv.UV_WRITABLE

def get_version():
    uv_bytes = ffi.string(libuv.uv_version_string())
    if not isinstance(uv_bytes, str):
        # Py3
        uv_str = uv_bytes.decode("ascii")
    else:
        uv_str = uv_bytes

    return 'libuv-' + uv_str

def get_header_version():
    return 'libuv-%d.%d.%d' % (libuv.UV_VERSION_MAJOR, libuv.UV_VERSION_MINOR, libuv.UV_VERSION_PATCH)

def supported_backends():
    return ['default']


class loop(AbstractLoop):

    # XXX: Undocumented. Maybe better named 'timer_resolution'? We can't
    # know this in general on libev
    min_sleep_time = 0.001 # 1ms

    DEFAULT_LOOP_REGENERATES = True

    error_handler = None

    _CHECK_POINTER = 'uv_check_t *'

    _PREPARE_POINTER = 'uv_prepare_t *'
    _PREPARE_CALLBACK_SIG = "void(*)(void*)"

    _TIMER_POINTER = _CHECK_POINTER # This is poorly named. It's for the callback "timer"

    def __init__(self, flags=None, default=None):
        AbstractLoop.__init__(self, ffi, libuv, _watchers, flags, default)
        self.__loop_pid = os.getpid()
        self._child_watchers = defaultdict(list)
        self._io_watchers = dict()
        self._fork_watchers = set()
        self._pid = os.getpid()

    def _init_loop(self, flags, default):
        if default is None:
            default = True
            # Unlike libev, libuv creates a new default
            # loop automatically if the old default loop was
            # closed.

        if default:
            ptr = libuv.uv_default_loop()
        else:
            ptr = libuv.uv_loop_new()


        if not ptr:
            raise SystemError("Failed to get loop")
        return ptr

    _signal_idle = None

    def _init_and_start_check(self):
        libuv.uv_check_init(self._ptr, self._check)
        libuv.uv_check_start(self._check, libuv.python_check_callback)
        libuv.uv_unref(self._check)
        _dbg("Started check watcher", ffi.cast('void*', self._check))

        # We also have to have an idle watcher to be able to handle
        # signals in a timely manner. Without them, libuv won't loop again
        # and call into its check and prepare handlers.
        # Note that this basically forces us into a busy-loop
        # XXX: As predicted, using an idle watcher causes our process
        # to eat 100% CPU time. We instead use a timer with a max of a 1 second
        # delay to notice signals. Note that this timeout also implements fork
        # watchers, effectively.

        # XXX: Perhaps we could optimize this to notice when there are other
        # timers in the loop and start/stop it then. When we have a callback
        # scheduled, this should also be the same and unnecessary?
        # libev does takes this basic approach on Windows.
        self._signal_idle = ffi.new("uv_timer_t*")
        libuv.uv_timer_init(self._ptr, self._signal_idle)
        self._signal_idle.data = self._handle_to_self
        libuv.uv_timer_start(self._signal_idle, libuv.python_check_callback,
                             300,
                             300)
        libuv.uv_unref(self._signal_idle)

    def _run_callbacks(self):
        # Manually handle fork watchers.
        curpid = os.getpid()
        if curpid != self._pid:
            self._pid = curpid
            for watcher in self._fork_watchers:
                watcher._on_fork()
        super(loop, self)._run_callbacks()

    def _init_and_start_prepare(self):
        libuv.uv_prepare_init(self._ptr, self._prepare)
        libuv.uv_prepare_start(self._prepare, libuv.python_prepare_callback)
        libuv.uv_unref(self._prepare)

    def _init_callback_timer(self):
        libuv.uv_check_init(self._ptr, self._timer0)

    def _stop_callback_timer(self):
        libuv.uv_check_stop(self._timer0)

    def _start_callback_timer(self):
        # The purpose of the callback timer is to ensure that we run
        # callbacks as soon as possible on the next iteration of the event loop.

        # In libev, we set a 0 duration timer with a no-op callback.
        # This executes immediately *after* the IO poll is done (it
        # actually determines the time that the IO poll will block
        # for), so having the timer present simply spins the loop, and
        # our normal prepare watcher kicks in to run the callbacks.

        # In libuv, however, timers are run *first*, before prepare
        # callbacks and before polling for IO. So a no-op 0 duration
        # timer actually does *nothing*. (Also note that libev queues all
        # watchers found during IO poll to run at the end (I think), while libuv
        # runs them in uv__io_poll itself.)

        # From the loop inside uv_run:
        # while True:
        #   uv__update_time(loop);
        #   uv__run_timers(loop);
        #   # we don't use pending watchers. They are how libuv
        #   # implements the pipe/udp/tcp streams.
        #   ran_pending = uv__run_pending(loop);
        #   uv__run_idle(loop);
        #   uv__run_prepare(loop);
        #   ...
        #   uv__io_poll(loop, timeout); # <--- IO watchers run here!
        #   uv__run_check(loop);

        # libev looks something like this (pseudo code because the real code is
        # hard to read):
        #
        # do {
        #    run_fork_callbacks();
        #    run_prepare_callbacks();
        #    timeout = min(time of all timers or normal block time)
        #    io_poll() # <--- Only queues IO callbacks
        #    update_now(); calculate_expired_timers();
        #    run callbacks in this order: (although specificying priorities changes it)
        #        check
        #        stat
        #        child
        #        signal
        #        timer
        #        io
        # }

        # So instead of running a no-op and letting the side-effect of spinning
        # the loop run the callbacks, we must explicitly run them here.

        # If we don't, test__systemerror:TestCallback will be flaky, failing
        # one time out of ~20, depending on timing.

        # To get them to run immediately after this current loop,
        # we use a check watcher, instead of a 0 duration timer entirely.
        # If we use a 0 duration timer, we can get stuck in a timer loop.
        # Python 3.6 fails in test_ftplib.py

        # As a final note, if we have not yet entered the loop *at
        # all*, and a timer was created with a duration shorter than
        # the amount of time it took for us to enter the loop in the
        # first place, it may expire and get called before our callback
        # does. This could also lead to test__systemerror:TestCallback
        # appearing to be flaky.

        # As yet another final note, if we are currently running a
        # timer callback, meaning we're inside uv__run_timers() in C,
        # and the Python starts a new timer, if the Python code then
        # update's the loop's time, it's possible that timer will
        # expire *and be run in the same iteration of the loop*. This
        # is trivial to do: In sequential code, anything after
        # `gevent.sleep(0.1)` is running in a timer callback. Starting
        # a new timer---e.g., another gevent.sleep() call---will
        # update the time, *before* uv__run_timers exits, meaning
        # other timers get a chance to run before our check or prepare
        # watcher callbacks do. Therefore, we do indeed have to have a 0
        # timer to run callbacks---it gets inserted before any other user
        # timers---ideally, this should be especially careful about how much time
        # it runs for.

        # AND YET: We can't actually do that. We get timeouts that I haven't fully
        # investigated if we do. Probably stuck in a timer loop.

        # As a partial remedy to this, unlike libev, our timer watcher
        # class doesn't update the loop time by default.

        libuv.uv_check_start(self._timer0, libuv.python_prepare_callback)


    def _stop_aux_watchers(self):
        libuv.uv_prepare_stop(self._prepare)
        libuv.uv_ref(self._prepare) # Why are we doing this?
        libuv.uv_check_stop(self._check)
        libuv.uv_ref(self._check)

        libuv.uv_timer_stop(self._signal_idle)
        libuv.uv_ref(self._signal_idle)

    def _setup_for_run_callback(self):
        self._start_callback_timer()
        libuv.uv_ref(self._timer0)

    def destroy(self):
        if self._ptr:
            ptr = self._ptr
            super(loop, self).destroy()

            assert self._ptr is None
            libuv.uv_stop(ptr)
            closed_failed = libuv.uv_loop_close(ptr)
            if closed_failed:
                assert closed_failed == libuv.UV_EBUSY
                # Walk the open handlers, close them, then
                # run the loop once to clear them out and
                # close again.

                def walk(handle, _arg):
                    if not libuv.uv_is_closing(handle):
                        libuv.uv_close(handle, ffi.NULL)

                libuv.uv_walk(ptr,
                              ffi.callback("void(*)(uv_handle_t*,void*)",
                                           walk),
                              ffi.NULL)

                ran_has_more_callbacks = libuv.uv_run(ptr, libuv.UV_RUN_ONCE)
                if ran_has_more_callbacks:
                    libuv.uv_run(ptr, libuv.UV_RUN_NOWAIT)
                closed_failed = libuv.uv_loop_close(ptr)
                assert closed_failed == 0, closed_failed

            # XXX: Do we need to uv_loop_delete the non-default loop?
            # Probably...

    def debug(self):
        """
        Return all the handles that are open and their ref status.
        """

        # XXX: Disabled because, at least on Windows, the times this
        # gets called often produce `SystemError: ffi.from_handle():
        # dead or bogus handle object`, and sometimes that crashes the process.
        return []

    def _really_debug(self):
        handle_state = namedtuple("HandleState",
                                  ['handle',
                                   'watcher',
                                   'ref',
                                   'active',
                                   'closing'])
        handles = []

        def walk(handle, _arg):
            data = handle.data
            if data:
                watcher = ffi.from_handle(data)
            else:
                watcher = None
            handles.append(handle_state(handle,
                                        watcher,
                                        libuv.uv_has_ref(handle),
                                        libuv.uv_is_active(handle),
                                        libuv.uv_is_closing(handle)))

        libuv.uv_walk(self._ptr,
                      ffi.callback("void(*)(uv_handle_t*,void*)",
                                   walk),
                      ffi.NULL)
        return handles

    def ref(self):
        pass

    def unref(self):
        # XXX: Called by _run_callbacks.
        pass

    def break_(self, how=None):
        libuv.uv_stop(self._ptr)

    def reinit(self):
        # TODO: How to implement? We probably have to simply
        # re-__init__ this whole class? Does it matter?
        # OR maybe we need to uv_walk() and close all the handles?

        # XXX: libuv < 1.12 simply CANNOT handle a fork unless you immediately
        # exec() in the child. There are multiple calls to abort() that
        # will kill the child process:
        # - The OS X poll implementation (kqueue) aborts on an error return
        # value; since kqueue FDs can't be inherited, then the next call
        # to kqueue in the child will fail and get aborted; fork() is likely
        # to be called during the gevent loop, meaning we're deep inside the
        # runloop already, so we can't even close the loop that we're in:
        # it's too late, the next call to kqueue is already scheduled.
        # - The threadpool, should it be in use, also aborts
        # (https://github.com/joyent/libuv/pull/1136)
        # - There global shared state that breaks signal handling
        # and leads to an abort() in the child, EVEN IF the loop in the parent
        # had already been closed
        # (https://github.com/joyent/libuv/issues/1405)

        # In 1.12, the uv_loop_fork function was added (by gevent!)
        libuv.uv_loop_fork(self._ptr)


    def run(self, nowait=False, once=False):
        _dbg("Entering libuv.uv_run")
        # we can only respect one flag or the other.
        # nowait takes precedence because it can't block
        mode = libuv.UV_RUN_DEFAULT
        if once:
            mode = libuv.UV_RUN_ONCE
        if nowait:
            mode = libuv.UV_RUN_NOWAIT

        # if mode == libuv.UV_RUN_DEFAULT:
        #     print("looping in python")
        #     ptr = self._ptr
        #     ran_error = 0
        #     while ran_error == 0:
        #         ran_error = libuv.uv_run(ptr, libuv.UV_RUN_ONCE)
        #     if ran_error != 0:
        #         print("Error running loop", libuv.uv_err_name(ran_error),
        #               libuv.uv_strerror(ran_error))
        #     return ran_error
        return libuv.uv_run(self._ptr, mode)

    def now(self):
        # libuv's now is expressed as an integer number of
        # milliseconds, so to get it compatible with time.time units
        # that this method is supposed to return, we have to divide by 1000.0
        now = libuv.uv_now(self._ptr)
        return now / 1000.0

    def update_now(self):
        libuv.uv_update_time(self._ptr)

    @property
    def default(self):
        return self._ptr == libuv.uv_default_loop()

    def fileno(self):
        if self._ptr:
            fd = libuv.uv_backend_fd(self._ptr)
            if fd >= 0:
                return fd

    _sigchld_watcher = None
    _sigchld_callback_ffi = None

    def install_sigchld(self):
        if not self.default:
            return

        if self._sigchld_watcher:
            return

        self._sigchld_watcher = ffi.new('uv_signal_t*')
        libuv.uv_signal_init(self._ptr, self._sigchld_watcher)
        self._sigchld_watcher.data = self._handle_to_self

        libuv.uv_signal_start(self._sigchld_watcher,
                              libuv.python_sigchld_callback,
                              signal.SIGCHLD)

    def reset_sigchld(self):
        if not self.default or not self._sigchld_watcher:
            return

        libuv.uv_signal_stop(self._sigchld_watcher)
        # Must go through this to manage the memory lifetime
        # correctly. Alternately, we could just stop it and restart
        # it in install_sigchld?
        _watchers.watcher._watcher_ffi_close(self._sigchld_watcher)
        del self._sigchld_watcher


    def _sigchld_callback(self):
        while True:
            try:
                pid, status, _usage = os.wait3(os.WNOHANG)
            except OSError:
                # Python 3 raises ChildProcessError
                break

            if pid == 0:
                break
            children_watchers = self._child_watchers.get(pid, []) + self._child_watchers.get(0, [])
            for watcher in children_watchers:
                watcher._set_status(status)


    def io(self, fd, events, ref=True, priority=None):
        # We rely on hard references here and explicit calls to
        # close() on the returned object to correctly manage
        # the watcher lifetimes.

        io_watchers = self._io_watchers
        try:
            io_watcher = io_watchers[fd]
            assert io_watcher._multiplex_watchers, ("IO Watcher %s unclosed but should be dead" % io_watcher)
        except KeyError:
            # Start the watcher with just the events that we're interested in.
            # as multiplexers are added, the real event mask will be updated to keep in sync.
            # If we watch for too much, we get spurious wakeups and busy loops.
            io_watcher = self._watchers.io(self, fd, 0)
            io_watchers[fd] = io_watcher
            io_watcher._no_more_watchers = lambda: delitem(io_watchers, fd)

        return io_watcher.multiplex(events)

    def timer(self, after, repeat=0.0, ref=True, priority=None):
        if after <= 0 and repeat <= 0:
            # Make sure we can spin the loop. See timer.
            # XXX: Note that this doesn't have a `again` method.
            return self._watchers.OneShotCheck(self, ref, priority)
        return super(loop, self).timer(after, repeat, ref, priority)
