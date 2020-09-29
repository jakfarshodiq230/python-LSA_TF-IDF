from ....utils import observer_pool, py3
from . import synchronizing

import collections


def _as_name_list(lst):
    if lst is None:
        return None
    elif isinstance(lst,py3.textstring):
        return [lst]
    return lst
TSignal=collections.namedtuple("TSignal",["src","tag","value"])
class SignalPool(object):
    """
    Signal dispatcher (somewhat similar in functionality to Qt signals).

    Manages dispatching signals between sources and destinations (callback functions).
    Each signal has defined source, destination (both can also be ``"all"`` or ``"any"``, see methods descriptions for details), tag and value.
    Any thread can send a signal or subscribe for a signal with given filters (source, destination, tag, additional filters).
    If a signal is emitted, it is checked against filters for all subscribers, and the passing ones are then called.
    """
    def __init__(self):
        object.__init__(self)
        self._pool=observer_pool.ObserverPool()

    def subscribe_nonsync(self, callback, srcs="any", dsts="any", tags=None, filt=None, priority=0, id=None):
        """
        Subscribe asynchronous callback to a signal.

        If signal is sent, `callback` is called from the sending thread (not subscribed thread). Therefore, should be used with care.
        In Qt, analogous to making signal connection with a direct call.

        Args:
            callback: callback function, which takes 3 arguments: signal source, signal tag, signal value.
            src(str or [str]): signal source or list of sources (controller names) to filter the subscription;
                can be ``"any"`` (any source) or ``"all"`` (only signals specifically having ``"all"`` as a source).
            src(str or [str]): signal destination or list of destinations (controller names) to filter the subscription;
                can be ``"any"`` (any destination) or ``"all"`` (only source specifically having ``"all"`` as a destination).
            tags: signal tag or list of tags to filter the subscription (any tag by default).
            filt(callable): additional filter function which takes 4 arguments: signal source, signal destination, signal tag, signal value,
                and checks whether signal passes the requirements.
            priority(int): subscription priority (higher priority subscribers are called first).
            id(int): subscription ID (by default, generate a new unique name).

        Returns:
            subscription ID, which can be used to unsubscribe later.
        """
        srcs=_as_name_list(srcs)
        dsts=_as_name_list(dsts)
        tags=_as_name_list(tags)
        src_any="any" in srcs
        dst_any="any" in dsts
        def full_filt(tag, value):
            src,dst,tag=tag
            if (tags is not None) and (tag is not None) and (tag not in tags):
                return False
            if (not src_any) and (src!="all") and (src not in srcs):
                return False
            if (not dst_any) and (dst!="all") and (dst not in dsts):
                return False
            return filt(src,dst,tag,value) if (filt is not None) else True
        return self._pool.add_observer(callback,name=id,filt=full_filt,priority=priority,cacheable=(filt is None))
    def subscribe(self, callback, srcs="any", dsts="any", tags=None, filt=None, priority=0, limit_queue=1, limit_period=0, dest_controller=None, call_tag=None, add_call_info=False, id=None):
        """
        Subscribe synchronous callback to a signal.

        If signal is sent, `callback` is called from the `dest_controller` thread (by default, thread which is calling this function).
        In Qt, analogous to making signal connection with a queued call.
        
        Args:
            callback: callback function, which takes 3 arguments: signal source, signal tag, signal value.
            srcs(str or [str]): signal source name or list of source names to filter the subscription;
                can be ``"any"`` (any source) or ``"all"`` (only signals specifically having ``"all"`` as a source).
            dsts(str or [str]): signal destination name or list of destination names to filter the subscription;
                can be ``"any"`` (any destination) or ``"all"`` (only source specifically having ``"all"`` as a destination).
            tags: signal tag or list of tags to filter the subscription (any tag by default).
            filt(callable): additional filter function which takes 4 arguments: signal source, signal destination, signal tag, signal value,
                and checks whether signal passes the requirements.
            priority(int): subscription priority (higher priority subscribers are called first).
            limit_queue(int): limits the maximal number of scheduled calls
                (if the signal is sent while at least `limit_queue` callbacks are already in queue to be executed, ignore it).
            limit_period(float): limits the minimal time between two call to the subscribed callback
                (if the signal is sent less than `limit_period` after the previous signal, ignore it).
            call_tag(str or None): tag used for the synchronized call; by default, use the interrupt call (which is the default of ``call_in_thread``).
            add_call_info(bool): if ``True``, add a fourth argument containing a call information (see :class:`.TSignalSynchronizerInfo` for details).
            id(int): subscription ID (by default, generate a new unique name).

        Returns:
            subscription ID, which can be used to unsubscribe later.
        """
        sync_callback=synchronizing.SignalSynchronizer(callback,limit_queue=limit_queue,limit_period=limit_period,add_call_info=add_call_info,dest_controller=dest_controller,call_tag=call_tag)
        return self.subscribe_nonsync(sync_callback,srcs=srcs,dsts=dsts,tags=tags,filt=filt,priority=priority,id=id)
    def unsubscribe(self, id):
        """Unsubscribe from a subscription with a given ID."""
        self._pool.remove_observer(id)

    def signal(self, src, dst="any", tag=None, value=None):
        """
        Send a signal.

        Args:
            src(str): signal source; can be a name, ``"all"`` (will pass all subscribers' source filters),
                or ``"any"`` (will only be passed to subscribers specifically subscribed to signal with ``"any"`` source).
            dst(str): signal destination; can be a name, ``"all"`` (will pass all subscribers' destination filters),
                or ``"any"`` (will only be passed to subscribers specifically subscribed to signal with ``"any"`` destination).
            tag(str): signal tag.
            value: signal value.
        """
        to_call=self._pool.find_observers(TSignal(src,dst,tag),value)
        for _,obs in to_call:
            obs.callback(src,tag,value)