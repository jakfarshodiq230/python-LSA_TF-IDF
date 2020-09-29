from ....utils import general, funcargparse, dictionary, functions as func_utils
from . import signal_pool as spool, threadprop, synchronizing

from PyQt5 import QtCore

import threading
import contextlib
import time
import sys, traceback
import heapq
    
_depends_local=[".signal_pool",".synchronizing","....utils.functions"]

_default_signal_pool=spool.SignalPool()

_created_threads={}
_running_threads={}
_stopped_threads={}
_running_threads_lock=threading.Lock()
_running_threads_notifier=synchronizing.QMultiThreadNotifier()
_running_threads_stopping=False


_exception_print_lock=threading.Lock()


@contextlib.contextmanager
def exint(error_msg_template="{}:"):
    """Context that intercepts exceptions and stops the execution in a controlled manner (quitting the main thread)"""
    try:
        yield
    except threadprop.InterruptExceptionStop:
        pass
    except:
        with _exception_print_lock:
            try:
                ctl_name=get_controller(wait=False).name
                print(error_msg_template.format("Exception raised in thread '{}'".format(ctl_name)),file=sys.stderr)
            except threadprop.NoControllerThreadError:
                print(error_msg_template.format("Exception raised in an uncontrolled thread"),file=sys.stderr)
            traceback.print_exc()
            sys.stderr.flush()
        try:
            stop_controller("gui",code=1,sync=False,require_controller=True)
        except threadprop.NoControllerThreadError:
            with _exception_print_lock:
                print("Can't stop GUI thread; quitting the application",file=sys.stderr)
                sys.stderr.flush()
            sys.exit(1)
        except threadprop.InterruptExceptionStop:
            pass

def exsafe(func):
    """Decorator that intercepts exceptions raised by `func` and stops the execution in a controlled manner (quitting the main thread)"""
    error_msg_template="{{}} executing function '{}':".format(func.__name__)
    @func_utils.getargsfrom(func,hide_outer_obj=True) # PyQt slots don't work well with bound methods
    def safe_func(*args, **kwargs):
        with exint(error_msg_template=error_msg_template):
            return func(*args,**kwargs)
    return safe_func
def exsafeSlot(*slargs, **slkwargs):
    """Wrapper around ``PyQt5.QtCore.pyqtSlot`` which intercepts exceptions and stops the execution in a controlled manner"""
    def wrapper(func):
        return QtCore.pyqtSlot(*slargs,**slkwargs)(exsafe(func))
    return wrapper

class QThreadControllerThread(QtCore.QThread):
    finalized=QtCore.pyqtSignal()
    _stop_request=QtCore.pyqtSignal()
    def __init__(self, controller):
        QtCore.QThread.__init__(self)
        self.moveToThread(self)
        self.controller=controller
        threadprop.get_app().aboutToQuit.connect(self.quit_sync)
        self._stop_request.connect(self._do_quit)
        self._stop_requested=False
    def run(self):
        with exint():
            try:
                self.exec_() # main execution event loop
            finally:
                self.finalized.emit()
                self.exec_() # finalizing event loop (exitted after finalizing event is processed)
    @QtCore.pyqtSlot()
    def _do_quit(self):
        if self.isRunning() and not self._stop_requested:
            self.controller.request_stop() # signal controller to stop
            self.quit() # quit the first event loop
            self._stop_requested=True
    def quit_sync(self):
        self._stop_request.emit()


def remote_call(func):
    """Decorator that turns a controller method into a remote call (call from a different thread is passed synchronously)"""
    @func_utils.getargsfrom(func)
    def rem_func(self, *args, **kwargs):
        return self.call_in_thread_sync(func,args=(self,)+args,kwargs=kwargs,sync=True,same_thread_shortcut=True)
    return rem_func

def call_in_thread(thread_name):
    """Decorator that turns any function into a remote call in a thread with a given name (call from a different thread is passed synchronously)"""
    def wrapper(func):
        @func_utils.getargsfrom(func)
        def rem_func(*args, **kwargs):
            thread=get_controller(thread_name)
            return thread.call_in_thread_sync(func,args=args,kwargs=kwargs,sync=True,same_thread_shortcut=True)
        return rem_func
    return wrapper
call_in_gui_thread=call_in_thread("gui")
"""Decorator that turns any function into a remote call in a GUI thread (call from a different thread is passed synchronously)"""




class QThreadController(QtCore.QObject):
    """
    Generic Qt thread controller.

    Responsible for all inter-thread synchronization. There is one controller per thread, and 

    Args:
        name(str): thread name (by default, generate a new unique name);
            this name can be used to obtain thread controller via :func:`get_controller`
        kind(str): thread kind; can be ``"loop"`` (thread is running in the Qt message loop; behavior is implemented in :meth:`process_message` and remote calls),
            ``"run"`` (thread executes :meth:`run` method and quits after it is complete), or ``"main"`` (can only be created in the main GUI thread)
        signal_pool: :class:`.SignalPool` for this thread (by default, use the default common pool)

    Methods to overload:
        on_start: executed on the thread startup (between synchronization points ``"start"`` and ``"run"``)
        on_finish: executed on thread cleanup (attempts to execute in any case, including exceptions)
        run: executed once per thread; thread is stopped afterwards (only if ``kind=="run"``)
        process_message: function that takes 2 arguments (tag and value) of the message and processes it; returns ``True`` if the message has been processed and ``False`` otherwise (in which case it is stored and can be recovered via :meth:`wait_for_message`/:meth:`pop_message`); by default, always return ``False``
        process_interrupt: function that tales 2 arguments (tag and value) of the interrupt message (message with a tag starting with ``"interrupt."``) and processes it; by default, assumes that any value with tag ``"execute"`` is a function and executes it
    
    Signals:
        started: emitted on thread start (after :meth:`on_start` is executed)
        finished: emitted on thread finish (before :meth:`on_finish` is executed)
    """
    def __init__(self, name=None, kind="loop", signal_pool=None):
        QtCore.QObject.__init__(self)
        funcargparse.check_parameter_range(kind,"kind",{"loop","run","main"})
        if kind=="main":
            name="gui"
        self.name=name or threadprop.thread_uids(type(self).__name__)
        self.kind=kind
        # register thread
        _store_created_controller(self)
        if self.kind=="main":
            if not threadprop.is_gui_thread():
                raise threadprop.ThreadError("GUI thread controller can only be created in the main thread")
            if threadprop.current_controller(require_controller=False):
                raise threadprop.DuplicateControllerThreadError()
            self.thread=threadprop.get_gui_thread()
            threadprop.local_data.controller=self
            _register_controller(self)
        else:
            self.thread=QThreadControllerThread(self)

        # set up message processing
        self._wait_timer=QtCore.QBasicTimer()
        self._message_queue={}
        self._message_uid=0
        self._sync_queue={}
        self._sync_clearance=set()
        # set up variable handling
        self._params_val=dictionary.Dictionary()
        self._params_val_lock=threading.Lock()
        self._params_exp=dictionary.Dictionary()
        self._params_exp_lock=threading.Lock()
        # set up high-level synchronization
        self._exec_notes={}
        self._exec_notes_lock=threading.Lock()
        self._signal_pool=signal_pool or _default_signal_pool
        self._signal_pool_uids=[]
        self._stop_notifiers=[]
        # set up life control
        self._stop_requested=(self.kind!="main")
        self._lifetime_state_lock=threading.Lock()
        self._lifetime_state="stopped"
        # set up signals
        self.moveToThread(self.thread)
        self._messaged.connect(self._get_message,QtCore.Qt.QueuedConnection)
        self._interrupt_called.connect(self._on_call_in_thread,QtCore.Qt.QueuedConnection)
        if self.kind=="main":
            threadprop.get_app().aboutToQuit.connect(self._on_finish_event,type=QtCore.Qt.DirectConnection)
            threadprop.get_app().lastWindowClosed.connect(self._on_last_window_closed,type=QtCore.Qt.DirectConnection)
            self._recv_started_event.connect(self._on_start_event,type=QtCore.Qt.QueuedConnection) # invoke delayed start event (call in the main loop)
            self._recv_started_event.emit()
            self._lifetime_state="setup"
        else:
            self.thread.started.connect(self._on_start_event,type=QtCore.Qt.QueuedConnection)
            self.thread.finalized.connect(self._on_finish_event,type=QtCore.Qt.QueuedConnection)
    
    ### Special signals processing ###
    _messaged=QtCore.pyqtSignal("PyQt_PyObject")
    @exsafeSlot("PyQt_PyObject")
    def _get_message(self, msg): # message signal processing
        kind,tag,priority,value=msg
        if kind=="msg":
            if tag.startswith("interrupt."):
                self.process_interrupt(tag[len("interrupt."):],value)
                return
            if self.process_message(tag,value):
                return
            mq=self._message_queue.setdefault(tag,[])
            heapq.heappush(mq,(priority,self._message_uid,value))
            self._message_uid+=1
        elif kind=="sync":
            if (tag,value) in self._sync_clearance:
                self._sync_clearance.remove((tag,value))
            else:
                self._sync_queue.setdefault(tag,set()).add(value)
        elif kind=="stop":
            with self._lifetime_state_lock:
                if self._lifetime_state!="finishing":
                    self._stop_requested=True
    _interrupt_called=QtCore.pyqtSignal("PyQt_PyObject")
    @exsafeSlot("PyQt_PyObject")
    def _on_call_in_thread(self, call): # call signal processing
        call()

    ### Execution starting / finishing ###
    _recv_started_event=QtCore.pyqtSignal()
    started=QtCore.pyqtSignal()
    @exsafeSlot()
    def _on_start_event(self):
        self._stop_requested=False
        with self._lifetime_state_lock:
            self._lifetime_state="setup"
        try:
            if self.kind!="main":
                threadprop.local_data.controller=self
                _register_controller(self)
            with self._lifetime_state_lock:
                self._lifetime_state="starting"
            self.notify_exec("start")
            self.on_start()
            self.started.emit()
            with self._lifetime_state_lock:
                self._lifetime_state="running"
            self.notify_exec("run")
            if self.kind=="run":
                self._do_run()
                self.thread.quit_sync()
        except threadprop.InterruptExceptionStop:
            self.thread.quit_sync()
    finished=QtCore.pyqtSignal()
    @exsafeSlot()
    def _on_finish_event(self):
        with self._lifetime_state_lock:
            self._lifetime_state="finishing"
        self._stop_requested=False
        self.finished.emit()
        try:
            self.check_messages()
            self.on_finish()
            self.check_messages()
        finally:
            if self.kind=="main":
                stop_all_controllers(stop_self=False)
            with self._lifetime_state_lock:
                self._lifetime_state="cleanup"
            for sn in self._stop_notifiers:
                sn()
            self._stop_notifiers=[]
            for uid in self._signal_pool_uids:
                self._signal_pool.unsubscribe(uid)
            self.notify_exec("stop")
            _unregister_controller(self)
            self.thread.quit() # stop event loop (no regular messages processed after this call)
            with self._lifetime_state_lock:
                self._lifetime_state="stopped"
    @QtCore.pyqtSlot()
    def _on_last_window_closed(self):
        if threadprop.get_app().quitOnLastWindowClosed():
            self.request_stop()



    ##########  INTERNAL CALLS  ##########
    ## Methods to be called by functions executing in the controlled thread ##

    ### Message loop management ###
    def _do_run(self):
        self.run()
    def _wait_in_process_loop(self, done_check, timeout=None):
        ctd=general.Countdown(timeout)
        while True:
            if self._stop_requested:
                raise threadprop.InterruptExceptionStop()
            if timeout is not None:
                time_left=ctd.time_left()
                if time_left:
                    self._wait_timer.start(max(int(time_left*1E3),1),self)
                    threadprop.get_app().processEvents(QtCore.QEventLoop.WaitForMoreEvents)
                    self._wait_timer.stop()
                else:
                    self.check_messages()
                    raise threadprop.TimeoutThreadError()
            else:
                threadprop.get_app().processEvents(QtCore.QEventLoop.WaitForMoreEvents)
            done,value=done_check()
            if done:
                return value
    def wait_for_message(self, tag, timeout=None):
        """
        Wait for a single message with a given tag.

        If timeout is passed, raise :exc:`.threadprop.TimeoutThreadError`.
        """
        def done_check():
            if self._message_queue.setdefault(tag,[]):
                value=heapq.heappop(self._message_queue[tag])[-1]
                return True,value
            return False,None
        return self._wait_in_process_loop(done_check,timeout=timeout)
    def new_messages_number(self, tag):
        """
        Get the number of queued messages with a given tag.
        """
        return len(self._message_queue.setdefault(tag,[]))
    def pop_message(self, tag):
        """
        Pop the latest message with the given tag.

        Select the message with the highest priority, and among those the oldest one.
        If no messages are available, raise :exc:`.threadprop.NoMessageThreadError`.
        """
        if self.new_messages_number(tag):
            return heapq.heappop(self._message_queue[tag])[-1]
        raise threadprop.NoMessageThreadError("no messages with tag '{}'".format(tag))
    def wait_for_sync(self, tag, uid, timeout=None):
        """
        Wait for synchronization signal with the given tag and UID.

        This method is rarely invoked directly, and is usually used by synchronizers code.
        If timeout is passed, raise :exc:`.threadprop.TimeoutThreadError`.
        """
        def done_check():
            if uid in self._sync_queue.setdefault(tag,set()):
                self._sync_queue[tag].remove(uid)
                return True,None
            return False,None
        try:
            self._wait_in_process_loop(done_check,timeout=timeout)
        except threadprop.TimeoutThreadError:
            self._sync_clearance.add((tag,uid))
            raise
    def wait_for_any_message(self, timeout=None):
        """
        Wait for any message (including synchronization messages or pokes).

        If timeout is passed, raise :exc:`.threadprop.TimeoutThreadError`.
        """
        self._wait_in_process_loop(lambda: (True,None),timeout=timeout)
    def wait_until(self, check, timeout=None):
        """
        Wait until a given condition is true.

        Condition is given by the `check` function, which is called after every new received message and should return ``True`` if the condition is met.
        If timeout is passed, raise :exc:`.threadprop.TimeoutThreadError`.
        """
        self._wait_in_process_loop(lambda: (check(),None),timeout=timeout)
    def check_messages(self):
        """
        Receive new messages.

        Runs the underlying message loop to process newly received message and signals (and place them in corresponding queues if necessary).
        This method is rarely invoked, and only should be used periodically during long computations to not 'freeze' the thread.
        """
        threadprop.get_app().processEvents(QtCore.QEventLoop.AllEvents)
        if self._stop_requested:
            raise threadprop.InterruptExceptionStop()
    def sleep(self, timeout):
        """
        Sleep for a given time (in seconds).

        Unlike :func:`time.sleep`, constantly checks the event loop for new messages (e.g., if stop or interrupt commands are issued).
        """
        try:
            self._wait_in_process_loop(lambda: (False,None),timeout=timeout)
        except threadprop.TimeoutThreadError:
            pass


    ### Overloaded methods for thread events ###
    def process_interrupt(self, tag, value):
        if tag=="execute":
            value()
    def process_message(self, tag, value):
        """
        Process a new message.

        If the function returns ``False``, the message is put in the corresponding queue.
        Otherwise, the the message is considered to be already, and it gets 'absorbed'.
        """
        return False
    def on_start(self):
        """Method invoked on the start of the thread."""
        pass
    def on_finish(self):
        """
        Method invoked in the end of the thread.
        
        Called regardless of the stopping reason (normal finishing, exception, application finishing).
        """
        pass
    def run(self):
        """Method called to run the main thread code (only for ``"run"`` thread kind)."""
        pass


    ### Managing signal pool interaction ###
    def subscribe(self, callback, srcs="any", dsts=None, tags=None, filt=None, priority=0, limit_queue=1, limit_period=0, add_call_info=False, id=None):
        """
        Subscribe synchronous callback to a signal.

        See :meth:`.SignalPool.subscribe` for details.
        By default, the subscribed destination is the thread's name.
        """
        if self._signal_pool:
            uid=self._signal_pool.subscribe(callback,srcs=srcs,dsts=dsts or self.name,tags=tags,filt=filt,priority=priority,
                limit_queue=limit_queue,limit_period=limit_period,add_call_info=add_call_info,dest_controller=self,id=id)
            self._signal_pool_uids.append(uid)
            return uid
    def subscribe_nonsync(self, callback, srcs="any", dsts=None, tags=None, filt=None, priority=0, id=None):
        """
        Subscribe asynchronous callback to a signal.

        See :meth:`.SignalPool.subscribe_nonsync` for details.
        By default, the subscribed destination is the thread's name.
        """
        if self._signal_pool:
            uid=self._signal_pool.subscribe_nonsync(callback,srcs=srcs,dsts=dsts or self.name,tags=tags,filt=filt,priority=priority,id=id)
            self._signal_pool_uids.append(uid)
            return uid
    def unsubscribe(self, id):
        """Unsubscribe from a subscription with a given ID."""
        self._signal_pool_uids.pop(id)
        self._signal_pool.unsubscribe(id)
    def send_signal(self, dst="any", tag=None, value=None, src=None):
        """
        Send a signal to the signal pool.

        See :meth:`.SignalPool.signal` for details.
        By default, the signal source is the thread's name.
        """
        self._signal_pool.signal(src or self.name,dst,tag,value)


    ### Variable management ###
    _variable_change_tag="#sync.wait.variable"
    def set_variable(self, name, value, notify=False, notify_tag="changed/*"):
        """
        Set thread variable.

        Can be called in any thread (controlled or external).
        If ``notify==True``, send a signal with the given `notify_tag` (where ``"*"`` symbol is replaced by the variable name).
        """
        with self._params_val_lock:
            self._params_val.add_entry(name,value,force=True)
        for ctl in self._params_exp.get(name,[]):
            ctl.send_message(self._variable_change_tag,value)
        if notify:
            notify_tag.replace("*",name)
            self.send_signal("any",notify_tag,value)
    def __setitem__(self, name, value):
        self.set_variable(name,value)
    def __delitem__(self, name):
        with self._params_val_lock:
            if name in self._params_val:
                del self._params_val[name]


    ##########  EXTERNAL CALLS  ##########
    ## Methods to be called by functions executing in other thread ##

    ### Message synchronization ###
    def send_message(self, tag, value, priority=0):
        """Send a message to the thread with a given tag, value and priority"""
        self._messaged.emit(("msg",tag,priority,value))
    def send_sync(self, tag, uid):
        """
        Send a synchronization signal with the given tag and UID.

        This method is rarely invoked directly, and is usually used by synchronizers code.
        """
        self._messaged.emit(("sync",tag,0,uid))


    ### Variables access ###
    def get_variable(self, name, default=None, copy_branch=True, missing_error=False):
        """
        Get thread variable.

        Can be called in any thread (controlled or external).
        If ``missing_error==False`` and no variable exists, return `default`; otherwise, raise and error.
        If ``copy_branch==True`` and the variable is a :class:`.Dictionary` branch, return its copy to ensure that it stays unaffected on possible further variable assignments.
        """
        with self._params_val_lock:
            if missing_error and name not in self._params_val:
                raise KeyError("no parameter {}".format(name))
            var=self._params_val.get(name,default)
            if copy_branch and dictionary.is_dictionary(var):
                var=var.copy()
        return var
    def __getitem__(self, name):
        return self.get_variable(name,missing_error=True)
    def wait_for_variable(self, name, pred, timeout=None):
        """
        Wait until thread variable with the given `name` satisfies the given condition.
        
        `pred` is a function which takes one argument (variable value) and returns whether the condition is satisfied.
        """
        if not hasattr(pred,"__call__"):
            v=pred
            if isinstance(pred,(tuple,list,set,dict)):
                pred=lambda x: x in v
            else:
                pred=lambda x: x==v
        ctl=threadprop.current_controller()
        with self._params_exp_lock:
            self._params_exp.setdefault(name,[]).append(ctl)
        ctd=general.Countdown(timeout)
        try:
            while True:
                value=self.get_variable(name)
                if pred(value):
                    return value
                ctl.wait_for_message(self._variable_change_tag,timeout=ctd.time_left())
        finally:
            with self._params_exp_lock:
                self._params_exp[name].remove(ctl)


    ### Thread execution control ###
    def start(self):
        """Start the thread."""
        self.thread.start()
    def request_stop(self):
        """Request thread stop (send a stop command)."""
        self._messaged.emit(("stop",None,0,None))
    def stop(self, code=0):
        """
        Stop the thread.

        If called from the thread, stop immediately by raising a :exc:`.qt.thread.threadprop.InterruptExceptionStop` exception. Otherwise, schedule thread stop.
        If the thread kind is ``"main"``, stop the whole application with the given exit code. Otherwise, stop the thread.
        """
        if self.kind=="main":
            def exit_main():
                threadprop.get_app().exit(code)
                self.request_stop()
            self.call_in_thread_callback(exit_main)
        else:
            self.thread.quit_sync()
        if threadprop.current_controller() is self:
            raise threadprop.InterruptExceptionStop
    def poke(self):
        """
        Send a dummy message to the thread.
        
        A cheap way to notify the thread that something happened (useful for, e.g., making thread leave :meth:`wait_for_any_message` method).
        """
        self._messaged.emit(("poke",None,0,None))
    def running(self):
        """Check if the thread is running."""
        return self._lifetime_state in {"starting","running","finishing"}
    

    def is_controlled(self):
        """Check if this controller corresponds to the current thread."""
        return QtCore.QThread.currentThread() is self.thread


    ### Notifier access ###
    def _get_exec_note(self, point):
        with self._exec_notes_lock:
            if point not in self._exec_notes:
                self._exec_notes[point]=synchronizing.QMultiThreadNotifier()
            return self._exec_notes[point]
    def notify_exec(self, point):
        """
        Mark the given execution point as passed.
        
        Automatically invoked points include ``"start"`` (thread starting), ``"run"`` (thread setup and ready to run), and ``"stop"`` (thread finished),
        but can be extended for arbitrary points.
        Any given point can be notified only once, the repeated notification causes error.
        """
        self._get_exec_note(point).notify()
    def sync_exec(self, point, timeout=None, counter=1):
        """
        Wait for the given execution point.
        
        Automatically invoked points include ``"start"`` (thread starting), ``"run"`` (thread setup and ready to run), and ``"stop"`` (thread finished).
        If timeout is passed, raise :exc:`.threadprop.TimeoutThreadError`.
        `counter` specifies the minimal number of pre-requisite :meth:`notify_exec` calls to finish the waiting (by default, a single call is enough).
        Return actual number of notifier calls up to date.
        """
        return self._get_exec_note(point).wait(timeout=timeout,state=counter)
    def add_stop_notifier(self, func, call_if_stopped=True):
        """
        Add stop notifier: a function which is called when the thread is about to be stopped (left the main message loop).

        The function is called in the controlled thread close to its shutdown, so it should be short, non-blocking, and thread-safe.
        If the thread is already stopped and ``call_if_stopped==True``, call `func` immediately (from the caller's thread).
        Return ``True`` if the thread is still running and the notifier is added, and ``False`` otherwise.
        """
        with self._lifetime_state_lock:
            if self._lifetime_state not in {"cleanup","stopped"}:
                if func not in self._stop_notifiers:
                    self._stop_notifiers.append(func)
                return True
        if call_if_stopped:
            func()
        return False
    def remove_stop_notifier(self, func):
        """
        Remove the stop notifier from this controller.

        Return ``True`` if the notifier was in this thread and is now removed, and ``False`` otherwise.
        """
        with self._lifetime_state_lock:
            try:
                self._stop_notifiers.remove(func)
                return True
            except ValueError:
                return False
    

    ### External call management ###
    def call_in_thread_callback(self, func, args=None, kwargs=None, callback=None, tag=None, priority=0):
        """
        Call a function in this thread with the given arguments.

        If `callback` is supplied, call it with the result as a single argument (call happens in the controller thread).
        If `tag` is supplied, send the call in a message with the given tag; otherwise, use the interrupt call (generally, higher priority method).
        """
        def call():
            res=func(*(args or []),**(kwargs or {}))
            if callback:
                callback(res)
        if tag is None:
            self._interrupt_called.emit(call)
        else:
            self.send_message(tag,call,priority=priority)
    def call_in_thread_sync(self, func, args=None, kwargs=None, sync=True, callback=None, timeout=None, default_result=None, pass_exception=True, tag=None, priority=0, error_on_stopped=True, same_thread_shortcut=True):
        """
        Call a function in this thread with the given arguments.

        If ``sync==True``, calling thread is blocked until the controlled thread executes the function, and the function result is returned
        (in essence, the fact that the function executes in a different thread is transparent).
        Otherwise, exit call immediately, and return a synchronizer object (:class:`.QThreadCallNotifier`),
        which can be used to check if the call is done (method `is_done`) and obtain the result (method :meth:`.QThreadCallNotifier.get_value_sync`).
        If `callback` is not ``None``, call it after the function is successfully executed (from the target thread), with a single parameter being function result.
        If ``pass_exception==True`` and `func` raises and exception, re-raise it in the caller thread (applies only if ``sync==True``).
        If `tag` is supplied, send the call in a message with the given tag and priority; otherwise, use the interrupt call (generally, higher priority method).
        If ``error_on_stopped==True`` and the controlled thread is stopped before it executed the call, raise :exc:`.qt.thread.threadprop.NoControllerThreadError`; otherwise, return `default_result`.
        If ``same_thread_shortcut==True`` (default), the call is synchronous, and the caller thread is the same as the controlled thread, call the function directly.
        """
        if callback:
            def call_func(*args, **kwargs):
                res=func(*args,**kwargs)
                callback(res)
                return res
        else:
            call_func=func
        if same_thread_shortcut and tag is None and sync and threadprop.current_controller() is self:
            return call_func(*(args or []),**(kwargs or {}))
        call=synchronizing.QSyncCall(call_func,args=args,kwargs=kwargs,pass_exception=pass_exception,error_on_fail=error_on_stopped)
        if self.add_stop_notifier(call.fail):
            call.set_callback(lambda: self.remove_stop_notifier(call.fail),call_on_fail=True)
        if tag is None:
            self._interrupt_called.emit(call)
        else:
            self.send_message(tag,call,priority=priority)
        return call.value(sync=sync,timeout=timeout,default=default_result)





class QMultiRepeatingThreadController(QThreadController):
    """
    Thread which allows to set up and run jobs and batch jobs with a certain time period, and execute commands in the meantime.

    Args:
        name(str): thread name (by default, generate a new unique name)
        signal_pool: :class:`.SignalPool` for this thread (by default, use the default common pool)

    Methods to overload:
        on_start: executed on the thread startup (between synchronization points ``"start"`` and ``"run"``)
        on_finish: executed on thread cleanup (attempts to execute in any case, including exceptions)
        check_commands: executed once a scheduling cycle to check for new commands / events and execute them
    """
    _new_jobs_check_period=0.02 # command refresh period if no jobs are scheduled (otherwise, after every job)
    def __init__(self, name=None, signal_pool=None):
        QThreadController.__init__(self,name,kind="run",signal_pool=signal_pool)
        self.sync_period=0
        self._last_sync_time=0
        self.jobs={}
        self.timers={}
        self._jobs_list=[]
        self.batch_jobs={}
        self._batch_jobs_args={}
        
    ### Job handling ###
    # Called only in the controlled thread #

    def add_job(self, name, job, period, initial_call=True):
        """
        Add a recurrent `job` which is called every `period` seconds.

        The job starts running automatically when the main thread loop start executing.
        If ``initial_call==True``, call `job` once immediately after adding.
        """
        if name in self.jobs:
            raise ValueError("job {} already exists".format(name))
        self.jobs[name]=job
        self.timers[name]=general.Timer(period)
        self._jobs_list.append(name)
        if initial_call:
            self._acknowledge_job(name)
            job()
    def change_job_period(self, name, period):
        """Change the period of the job `name`"""
        if name not in self.jobs:
            raise ValueError("job {} doesn't exists".format(name))
        self.timers[name].change_period(period)
    def remove_job(self, name):
        """Change the job `name` from the roster"""
        if name not in self.jobs:
            raise ValueError("job {} doesn't exists".format(name))
        self._jobs_list.remove(name)
        del self.jobs[name]
        del self.timers[name]

    def add_batch_job(self, name, job, cleanup=None):
        """
        Add a batch `job` which is executed once, but with continuations.

        After this call the job is just created, but is not running. To start it, call :meth:`start_batch_job`.
        If specified, `cleanup` is a finalizing function which is called both when the job terminates normally,
        and when it is forcibly stopped (including thread termination).

        Unlike the usual recurrent jobs, here `job` is a generator (usually defined by a function with ``yield`` statement).
        When the job is running, the generator is periodically called until it raises :exc:`StopIteration` exception, which signifies that the job is finished.
        From generator function point of view, after the job is started, the function is executed once normally,
        but every time ``yield`` statement is encountered, the execution is suspended for `period` seconds (specified in :meth:`start_batch_job`).
        """
        if name in self.jobs or name in self.batch_jobs:
            raise ValueError("job {} already exists".format(name))
        self.batch_jobs[name]=(job,cleanup)
    def start_batch_job(self, name, period, *args, **kwargs):
        """
        Start the batch job with the given name.

        `period` specifies suspension period. Optional arguments are passed to the job and the cleanup functions.
        """
        if name not in self.batch_jobs:
            raise ValueError("job {} doesn't exists".format(name))
        if name in self.jobs:
            self.stop_batch_job(name)
        self._batch_jobs_args[name]=(args,kwargs)
        job=self.batch_jobs[name][0]
        gen=job(*args,**kwargs)
        def do_step():
            try:
                p=next(gen)
                if p is not None:
                    self.change_job_period(name,p)
                return
            except StopIteration:
                pass
            self.stop_batch_job(name)
        self.add_job(name,do_step,period,initial_call=False)
    def batch_job_running(self, name):
        """Check if a given batch job running"""
        if name not in self.batch_jobs:
            raise ValueError("job {} doesn't exists".format(name))
        return name in self.jobs
    def stop_batch_job(self, name, error_on_stopped=False):
        """
        Stop a given batch job.
        
        If ``error_on_stopped==True`` and the job is not currently running, raise an error. Otherwise, do nothing.
        """
        if name not in self.batch_jobs:
            raise ValueError("job {} doesn't exists".format(name))
        if name not in self.jobs:
            if error_on_stopped:
                raise ValueError("job {} doesn't exists".format(name))
            return
        self.remove_job(name)
        args,kwargs=self._batch_jobs_args.pop(name)
        cleanup=self.batch_jobs[name][1]
        if cleanup:
            cleanup(*args,**kwargs)

    def subscribe_commsync(self, callback, srcs="any", dsts=None, tags=None, filt=None, priority=0, limit_queue=1, limit_period=0, add_call_info=False, id=None):
        """
        Subscribe callback to a signal which is synchronized with commands and jobs execution.

        Unlike the standard :meth:`.QThreadController.subscribe` method, the subscribed callback will only be executed between jobs or commands, not during one of these.

        See :meth:`.SignalPool.subscribe` for details.
        By default, the subscribed destination is the thread's name.
        """
        if self._signal_pool:
            uid=self._signal_pool.subscribe(callback,srcs=srcs,dsts=dsts or self.name,tags=tags,filt=filt,priority=priority,
                limit_queue=limit_queue,limit_period=limit_period,add_call_info=add_call_info,dest_controller=self,call_tag="control.execute",id=id)
            self._signal_pool_uids.append(uid)
            return uid

    def check_commands(self):
        """
        Check for commands to execute.

        Called once every scheduling cycle: after any recurrent or batch job, but at least every `self._new_jobs_check_period` seconds (by default 0.1s).
        By default, simply executed all commands passed in ``"control.execute"`` messages.
        """
        while self.new_messages_number("control.execute"):
            call=self.pop_message("control.execute")
            call()
    


    def _get_next_job(self, ct):
        if not self._jobs_list:
            return None,None
        name=None
        left=None
        for n in self._jobs_list:
            t=self.timers[n]
            l=t.time_left(ct)
            if l==0:
                name,left=n,0
                break
            elif (left is None) or (l<left):
                name,left=n,l
        return name,left
    def _acknowledge_job(self, name):
        try:
            idx=self._jobs_list.index(name)
            self._jobs_list.pop(idx)
            self._jobs_list.append(name)
            self.timers[name].acknowledge(nmin=1)
        except ValueError:
            pass
    def run(self):
        """Main scheduling loop"""
        while True:
            ct=time.time()
            name,to=self._get_next_job(ct)
            if name is None:
                self.sleep(self._new_jobs_check_period)
            else:
                run_job=True
                if (self._last_sync_time is None) or (self._last_sync_time+self.sync_period<=ct):
                    self._last_sync_time=ct
                    if not to:
                        self.check_messages()
                if to:
                    if to>self._new_jobs_check_period:
                        run_job=False
                        self.sleep(self._new_jobs_check_period)
                    else:
                        self.sleep(to)
                if run_job:
                    self._acknowledge_job(name)
                    job=self.jobs[name]
                    job()
            self.check_commands()

    def on_finish(self):
        QThreadController.on_finish(self)
        for n in self.batch_jobs:
            if n in self.jobs:
                self.stop_batch_job(n)


class QTaskThread(QMultiRepeatingThreadController):
    """
    Thread which allows to set up and run jobs and batch jobs with a certain time period, and execute commands in the meantime.

    Extension of :class:`QMultiRepeatingThreadController` with an interface for command creating and scheduling

    Args:
        name(str): thread name (by default, generate a new unique name)
        setupargs: args supplied to :math:`setup_task` method
        setupkwargs: keyword args supplied to :math:`setup_task` method
        signal_pool: :class:`.SignalPool` for this thread (by default, use the default common pool)

    Attributes:
        c: command accessor, which makes calls more function-like;
            ``ctl.c.comm(*args,**kwarg)`` is equivalent to ``ctl.call_command("comm",args,kwargs)``
        q: query accessor, which makes calls more function-like;
            ``ctl.q.comm(*args,**kwarg)`` is equivalent to ``ctl.call_query("comm",args,kwargs)``
        qs: query accessor which is made 'exception-safe' via :func:`exsafe` wrapper (i.e., safe to directly connect to slots)
            ``ctl.qi.comm(*args,**kwarg)`` is equivalent to ``with exint(): ctl.call_query("comm",args,kwargs)``
        qi: query accessor which ignores and silences any exceptions (including missing /stopped controller)
            useful for sending queries during thread finalizing / application shutdown, when it's not guaranteed that the query recipient is running
            (commands already ignore any errors, unless their results are specifically requested)

    Methods to overload:
        setup_task: executed on the thread startup (between synchronization points ``"start"`` and ``"run"``)
        finalize_task: executed on thread cleanup (attempts to execute in any case, including exceptions)
        process_signal: process a directed signal (signal with ``dst`` equal to this thread name); by default, does nothing
        process_command: process a command with given name and arguments; by default, check for commands set up with :meth:`add_command`
        process_query: process a query with given name and arguments; by default, check for commands set up with :meth:`add_command`
    """
    def __init__(self, name=None, setupargs=None, setupkwargs=None, signal_pool=None):
        QMultiRepeatingThreadController.__init__(self,name=name,signal_pool=signal_pool)
        self.setupargs=setupargs or []
        self.setupkwargs=setupkwargs or {}
        self._directed_signal.connect(self._on_directed_signal)
        self._commands={}
        self._command_priorities={}
        self.c=self.CommandAccess(self,sync=False)
        self.q=self.CommandAccess(self,sync=True)
        self.qs=self.CommandAccess(self,sync=True,safe=True)
        self.qi=self.CommandAccess(self,sync=True,safe=True,ignore_errors=True)

    ### Functions to be overloaded in subclasses ###
    def setup_task(self, *args, **kwargs):
        """Setup the thread (called before the main task loop)"""
        pass
    def process_signal(self, src, tag, value):
        """Process a named signal (with `dst` equal to the thread name) from the signal pool"""
        pass
    def process_command(self, name, *args, **kwargs):
        """Process a command call (by default, look for an earlier created command)"""
        return self._process_named_command(name,*args,**kwargs)
    def process_query(self, name, *args, **kwargs):
        """Process a query call (by default, look for an earlier created command)"""
        return self._process_named_command(name,*args,**kwargs)
    def finalize_task(self):
        """Finalize the thread (always called on thread termination, regardless of the reason)"""
        pass

    ### Status update function ###
    def update_status(self, kind, status, text=None, notify=True):
        """
        Update device status represented in thread variables.

        `kind` is the status kind and `status` is its value.
        Status variable name is ``"status/"+kind``.
        If ``text is not None``, it specifies new status text stored in ``"status/"+kind+"_text"``.
        If ``notify==True``, send a signal about the status change.
        """
        status_str="status/"+kind if kind else "status"
        self[status_str]=status
        if notify:
            self.send_signal("any",status_str,status)
        if text:
            self.set_variable(status_str+"_text",text)
            self.send_signal("any",status_str+"_text",text)

    ### Start/stop control (called automatically) ###
    def on_start(self):
        QMultiRepeatingThreadController.on_start(self)
        self.setup_task(*self.setupargs,**self.setupkwargs)
        self.subscribe_nonsync(self._recv_directed_signal)
    def on_finish(self):
        QMultiRepeatingThreadController.on_finish(self)
        self.finalize_task()

    _directed_signal=QtCore.pyqtSignal("PyQt_PyObject")
    @exsafeSlot("PyQt_PyObject")
    def _on_directed_signal(self, msg):
        self.process_signal(*msg)
    def _recv_directed_signal(self, tag, src, value):
        self._directed_signal.emit((tag,src,value))

    ### Command control ###    
    def add_command(self, name, command=None, priority=0):
        """
        Add a new command to the command set (by default same set applies both to commands and queries).

        If `command' is ``None``, look for the method with the given `name`; otherwise, it is a command function to be called.
        `priority` specifies command priority (if several commands are in a queue, higher-priority commands are executed first).
        """
        if name in self._commands:
            raise ValueError("command {} already exists".format(name))
        if command is None:
            command=getattr(self,name)
        self._commands[name]=command
        self._command_priorities[name]=priority
    def _process_named_command(self, name, *args, **kwargs):
        if name in self._commands:
            return self._commands[name](*args,**kwargs)
        else:
            raise KeyError("unrecognized command {}".format(name))


    ##########  EXTERNAL CALLS  ##########
    ## Methods to be called by functions executing in other thread ##

    ### Request calls ###
    def _sync_call(self, func, name, args, kwargs, sync, callback=None, timeout=None, priority=0, ignore_errors=False):
        priority=self._command_priorities.get(name,0)
        return self.call_in_thread_sync(func,args=(name,)+tuple(args or []),kwargs=kwargs,sync=sync,callback=callback,timeout=timeout,tag="control.execute",
                priority=priority,error_on_stopped=not ignore_errors,pass_exception=not ignore_errors)
    def call_command(self, name, args=None, kwargs=None, callback=None):
        """
        Invoke command call with the given name and arguments
        
        If `callback` is not ``None``, call it after the command is successfully executed (from the target thread), with a single parameter being the command result.
        Return :class:`.QThreadCallNotifier` object which can be used to wait for and read the command result.
        """
        return self._sync_call(self.process_command,name,args,kwargs,sync=False,callback=callback)
    def call_query(self, name, args=None, kwargs=None, timeout=None, ignore_errors=False):
        """
        Invoke query call with the given name and arguments, and return the result.
        
        Unlike :meth:`call_command`, wait until the call is done before returning.
        If ``ignore_errors==True``, ignore all possible problems with the call (controller stopped, call raised an exception) and return ``None`` instead.
        """
        return self._sync_call(self.process_query,name,args,kwargs,sync=True,timeout=timeout,ignore_errors=ignore_errors)

    class CommandAccess(object):
        """
        Accessor object designed to simplify command and query syntax.

        Automatically created by the thread, so doesn't need to be invoked externally.
        """
        def __init__(self, parent, sync, timeout=None, safe=False, ignore_errors=False):
            object.__init__(self)
            self.parent=parent
            self.sync=sync
            self.timeout=timeout
            self.safe=safe
            self.ignore_errors=ignore_errors
            self._calls={}
        def __getattr__(self, name):
            if name not in self._calls:
                parent=self.parent
                def remcall(*args, **kwargs):
                    if self.sync:
                        return parent.call_query(name,args,kwargs,timeout=self.timeout,ignore_errors=self.ignore_errors)
                    else:
                        return parent.call_command(name,args,kwargs)
                if self.safe:
                    remcall=exsafe(remcall)
                self._calls[name]=remcall
            return self._calls[name]




def _store_created_controller(controller):
    """
    Register a newly created controller.

    Called automatically on controller creation.
    """
    with _running_threads_lock:
        if _running_threads_stopping:
            raise threadprop.InterruptExceptionStop()
        name=controller.name
        if (name in _running_threads) or (name in _created_threads):
            raise threadprop.DuplicateControllerThreadError("thread with name {} already exists".format(name))
        _created_threads[name]=controller
def _register_controller(controller):
    """
    Register a controller as running.

    Called automatically on thread start.
    """
    with _running_threads_lock:
        if _running_threads_stopping:
            raise threadprop.InterruptExceptionStop()
        name=controller.name
        if name in _running_threads:
            raise threadprop.DuplicateControllerThreadError("thread with name {} already exists".format(name))
        if name not in _created_threads:
            raise threadprop.NoControllerThreadError("thread with name {} hasn't been created".format(name))
        _running_threads[name]=controller
        del _created_threads[name]
    _running_threads_notifier.notify()
def _unregister_controller(controller):
    """
    Remove a controller from the list of running controller.

    Called automatically on thread finish.
    """
    with _running_threads_lock:
        name=controller.name
        if name not in _running_threads:
            raise threadprop.NoControllerThreadError("thread with name {} doesn't exist".format(name))
        _stopped_threads[name]=controller
        del _running_threads[name]
    _running_threads_notifier.notify()



def get_controller(name=None, wait=True, timeout=None, sync=None):
    """
    Find a controller with a given name.

    If `name` is not supplied, yield current controller instead.
    If the controller is not present and ``wait==True``, wait (with the given timeout) until the controller is running;
    otherwise, raise error if the controller is not running.
    If `sync` is not ``None``, synchronize to the thread `sync` point (usually, ``"run"``) before returning.
    """
    if name is None:
        return threadprop.current_controller()
    with _running_threads_lock:
        if (not wait) and (name not in _running_threads):
            raise threadprop.NoControllerThreadError("thread with name {} doesn't exist".format(name))
    def wait_cond():
        with _running_threads_lock:
            if name in _running_threads:
                return _running_threads[name]
            if name in _stopped_threads:
                raise threadprop.NoControllerThreadError("thread with name {} is stopped".format(name))
    thread=_running_threads_notifier.wait_until(wait_cond,timeout=timeout)
    if sync is not None:
        thread.sync_exec(sync,timeout=timeout)
    return thread
def get_gui_controller(wait=False, timeout=None, create_if_missing=True):
    """
    Get GUI thread controller.

    If the controller is not present and ``wait==True``, wait (with the given timeout) until the controller is running.
    If the controller is still not present and ``create_if_missing==True``, initialize the standard GUI controller.
    """
    try:
        gui_ctl=get_controller("gui",wait=wait,timeout=timeout)
    except threadprop.NoControllerThreadError:
        if create_if_missing:
            gui_ctl=QThreadController("gui",kind="main")
        else:
            raise
    return gui_ctl


def stop_controller(name, code=0, sync=True, require_controller=False):
    """
    Stop a controller with a given name.

    `code` specifies controller exit code (only applies to the main thread controller).
    If ``require_controller==True`` and the controller is not present, raise and error; otherwise, do nothing.
    If ``sync==True``, wait until the controller is stopped.
    """
    try:
        controller=get_controller(name,wait=False)
        controller.stop(code=code)
        if sync:
            controller.sync_exec("stop")
            controller.thread.wait()
        return controller
    except threadprop.NoControllerThreadError:
        if require_controller:
            raise
def stop_all_controllers(sync=True, concurrent=True, stop_self=True):
    """
    Stop all running threads.

    If ``sync==True``, wait until the all of the controller are stopped.
    If ``sync==True`` and ``concurrent==True`` stop threads in concurrent manner (first issue stop messages to all of them, then wait until all are stopped).
    If ``sync==True`` and ``concurrent==False`` stop threads in consecutive manner (wait for each thread to stop before stopping the next one).
    If ``stop_self==True`` stop current thread after stopping all other threads.
    """
    global _running_threads_stopping
    with _running_threads_lock:
        _running_threads_stopping=True
        names=list(_running_threads.keys())
    current_ctl=get_controller().name
    if concurrent and sync:
        ctls=[]
        for n in names:
            if n!=current_ctl:
                ctls.append(stop_controller(n,sync=False))
        for ctl in ctls:
            if ctl:
                ctl.sync_exec("stop")
                ctl.thread.wait()
    else:
        for n in names:
            if (n!=current_ctl):
                stop_controller(n,sync=sync)
    if stop_self:
        stop_controller(current_ctl,sync=True)
def stop_app():
    """
    Initialize stopping the application.
    
    Do this either by stopping the GUI controller (if it exists), or by stopping all controllers.
    """
    try:
        get_gui_controller(create_if_missing=False).stop()
    except threadprop.NoControllerThreadError:
        stop_all_controllers(sync=False)