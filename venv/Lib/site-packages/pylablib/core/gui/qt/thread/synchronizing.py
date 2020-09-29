from . import threadprop
from ....mthread import notifier
from ....utils import general

import threading, time, collections


class QThreadNotifier(notifier.ISkippableNotifier):
    """
    Wait-notify thread synchronizer for controlled Qt threads based on :class:`.notifier.ISkippableNotifier`.

    Like :class:`.notifier.ISkippableNotifier`, the main functions are :meth:`.ISkippableNotifier.wait` (wait in a message loop until notified or until timeout expires)
    and :meth:`.ISkippableNotifier.notify` (notify the waiting thread). Both of these can only be called once and will raise and error otherwise.
    Along with notifying a variable can be passed, which can be accessed using :meth:`get_value` and :meth:`get_value_sync`.

    Args:
        skippable (bool): if ``True``, allows for skippable wait events
            (if :meth:`.ISkippableNotifier.notify` is called before :meth:`.ISkippableNotifier.wait`, neither methods are actually called).
    """
    _uid_gen=general.UIDGenerator(thread_safe=True)
    _notify_tag="#sync.notifier"
    def __init__(self, skippable=True):
        notifier.ISkippableNotifier.__init__(self,skippable=skippable)
        self._uid=None
        self.value=None
    def _pre_wait(self, *args, **kwargs):
        self._controller=threadprop.current_controller(require_controller=True)
        self._uid=self._uid_gen()
        return True
    def _do_wait(self, timeout=None):
        try:
            self._controller.wait_for_sync(self._notify_tag,self._uid,timeout=timeout)
            return True
        except threadprop.TimeoutThreadError:
            return False
    def _pre_notify(self, value=None):
        self.value=value
    def _do_notify(self, *args, **kwargs):
        self._controller.send_sync(self._notify_tag,self._uid)
        return True
    def get_value(self):
        """Get the value passed by the notifier (doesn't check if it has been passed already)"""
        return self.value
    def get_value_sync(self, timeout=None):
        """Wait (with the given `timeout`) for the value passed by the notifier"""
        if not self.done_wait():
            self.wait(timeout=timeout)
        return self.get_value()


class QMultiThreadNotifier(object):
    """
    Wait-notify thread synchronizer that can be used for multiple threads and called multiple times.

    Performs similar function to conditional variables.
    The synchronizer has an internal counter which is incread by 1 every time it is notified.
    The wait functions have an option to wait until the counter reaches the specific counter value (usually, 1 above the last wait call).
    """
    def __init__(self):
        object.__init__(self)
        self._lock=threading.Lock()
        self._cnt=0
        self._notifiers={}
    def wait(self, state=1, timeout=None):
        """
        Wait until notifier counter is equal to at least `state`
        
        Return current counter state plus 1, which is the next smallest value resulting in waiting.
        """
        with self._lock:
            if self._cnt>=state:
                return self._cnt+1
            n=QThreadNotifier()
            self._notifiers.setdefault(state,[]).append(n)
        success=n.wait(timeout=timeout)
        if success:
            return n.get_value()
        raise threadprop.TimeoutThreadError("synchronizer timed out")
    def wait_until(self, condition, timeout=None):
        """
        Wait until `condition` is met.

        `condition` is a function which is called (in the waiting thread) every time the synchronizer is notified.
        If it return non-``False``, the waiting is complete and its result is returned.
        """
        ctd=general.Countdown(timeout)
        cnt=1
        while True:
            res=condition()
            if res:
                return res
            cnt=self.wait(cnt,timeout=ctd.time_left())
    def notify(self):
        """Notify all waiting threads"""
        with self._lock:
            self._cnt+=1
            cnt=self._cnt
            notifiers=[]
            for k in list(self._notifiers):
                if k<=self._cnt:
                    notifiers+=self._notifiers.pop(k)
        for n in notifiers:
            n.notify(cnt)


class QThreadCallNotifier(QThreadNotifier):
    """
    Specific kind of :class:`QThreadNotifier` designed to notify about results of remote calls.

    Its :meth:`get_value_sync` function takes into account that remote call could fail or raise an exception.
    Used in (and could be returned by) :meth:`.QThreadController.call_in_thread_sync`.
    """
    def get_value_sync(self, timeout=None, default=None, error_on_fail=True, pass_exception=True):
        """
        Wait (with the given `timeout`) for the value passed by the notifier

        If ``pass_exception==True`` and the returned value represents exception, re-raise it; otherwise, return `default`.
        If ``error_on_fail==True`` and the controlled thread notifies of a fail (usually, if it's stopped before it executed the call),
        raise :exc:`.qt.thread.threadprop.NoControllerThreadError`; otherwise, return `default`.
        """
        res=QThreadNotifier.get_value_sync(self,timeout=timeout)
        if res:
            kind,value=res
            if kind=="result":
                return value
            elif kind=="exception":
                if pass_exception:
                    raise value
                else:
                    return default
            elif kind=="fail":
                if error_on_fail:
                    raise threadprop.NoControllerThreadError("failed executing remote call: controller is stopped")
                return default
            else:
                raise ValueError("unrecognized return value kind: {}".format(kind))
        else:
            if error_on_fail:
                raise threadprop.TimeoutThreadError
            return default

class QSyncCall(object):
    """
    Object representing a remote call in a different thread.

    Args:
        func: callable to be invoked in the destination thread
        args: arguments to be passed to `func`
        kwargs: keyword arguments to be passed to `func`
        pass_exception (bool): if ``True``, and `func` raises an exception, re-raise it in the caller thread
        error_on_fail (bool): if ``True`` and the controlled thread notifies of a fail (usually, if it's stopped before it executed the call),
                raise :exc:`.qt.thread.threadprop.NoControllerThreadError`; otherwise, return `default`.
    """
    def __init__(self, func, args=None, kwargs=None, pass_exception=True, error_on_fail=True):
        object.__init__(self)
        self.func=func
        self.args=args or []
        self.kwargs=kwargs or {}
        self.synchronizer=QThreadCallNotifier()
        self.pass_exception=pass_exception
        self.error_on_fail=error_on_fail
        self.callback=None
        self.callback_on_fail=True
    def __call__(self):
        try:
            res=("fail",None)
            res=("result",self.func(*self.args,**self.kwargs))
            if self.callback and not self.callback_on_fail:
                self.callback()
        except Exception as e:
            res=("exception",e)
            raise
        finally:
            if self.callback and self.callback_on_fail:
                self.callback()
            self.synchronizer.notify(res)
    def set_callback(self, callback, call_on_fail=True):
        """
        Set the callback to be executed after the main call is done.
        
        Callback is not provided with any arguments.
        If ``callback_on_fail==True``, call it even if the original call raised an exception.
        """
        self.callback=callback
        self.callback_on_fail=call_on_fail
    def fail(self):
        """Notify that the call is failed (invoked by the destination thread)"""
        self.synchronizer.notify(("fail",None))
    def value(self, sync=True, timeout=None, default=None):
        """
        Get the result of the call function (invoked by the caller thread).
        
        If ``sync==True``, wait until the execution is done and return the result.
        In this case `timeout` and `default` specify the waiting timeout and the default return value if the timeout is exceeded.
        If ``sync==False``, return :class:`QThreadCallNotifier` object which can be used to check execution/obtain the result later.
        """
        if sync:
            return self.synchronizer.get_value_sync(timeout=timeout,default=default,error_on_fail=self.error_on_fail,pass_exception=self.pass_exception)
        else:
            return self.synchronizer
    def wait(self, timeout=None):
        """Wait until the call is executed"""
        return self.synchronizer.wait(timeout)
    def done(self):
        """Check if the call is executed"""
        return self.synchronizer.done_wait()


TSignalSynchronizerInfo=collections.namedtuple("TSignalSynchronizerInfo",["call_time"])
class SignalSynchronizer(object):
    """
    Synchronizer used by :class:`.SignalPool` to manache sync signal subscriptions.

    Usually created by the signal pool, so it is rarely invoked directly.

    Args:
        func: function to be called when the signal arrives.
        limit_queue(int): limits the maximal number of scheduled calls
            (if the signal is sent while at least `limit_queue` callbacks are already in queue to be executed, ignore it).
        limit_period(float): limits the minimal time between two call to the subscribed callback
            (if the signal is sent less than `limit_period` after the previous signal, ignore it).
        add_call_info(bool): if ``True``, add a fourth argument containing a call information (see :class:`.TSignalSynchronizerInfo` for details).
        dest_controller: the controller for the thread in which `func` should be called; by default, it's the thread which creates the synchronizer object.
            call_tag(str or None): tag used for the synchronized call; by default, use the interrupt call (which is the default of ``call_in_thread``).
    """
    def __init__(self, func, limit_queue=1, limit_period=0, add_call_info=False, dest_controller=None, call_tag=None):
        dest_controller=dest_controller or threadprop.current_controller()
        self.call_tag=call_tag
        def call(*args):
            dest_controller.call_in_thread_callback(func,args,callback=self._call_done,tag=self.call_tag)
        self.call=call
        self.limit_queue=limit_queue
        self.queue_size=0
        self.limit_period=limit_period
        self.add_call_info=add_call_info
        self.last_call_time=None
        self.lock=threading.Lock()
    def _call_done(self, _):
        with self.lock:
            self.queue_size-=1
            
    def __call__(self, src, tag, value):
        t=time.time()
        with self.lock:
            if self.limit_queue and self.queue_size>=self.limit_queue:
                return
            if self.limit_period:
                if (self.last_call_time is not None) and (self.last_call_time+self.limit_period>t):
                    return
                self.last_call_time=t
            self.queue_size+=1
        if self.add_call_info:
            call_info=TSignalSynchronizerInfo(t)
            self.call(src,tag,value,call_info)
        else:
            self.call(src,tag,value)