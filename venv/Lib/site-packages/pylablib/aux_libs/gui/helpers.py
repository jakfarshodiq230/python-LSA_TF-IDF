from ...core.gui.qt.thread import controller
from ...core.utils import files as file_utils, general, funcargparse
from ...core.fileio import logfile

from PyQt5 import QtCore
import numpy as np
import threading
import collections
from future.utils import viewitems
import os.path

class StreamFormerThread(controller.QThreadController):
    """
    Thread that combines data from different sources and aligns it in complete rows.

    Channels can be added using :meth:`add_channel` function. Every time the new row is complete, it is added to the current block.
    When the block is complete (determined by ``block_period`` attribute), :meth:`on_new_block` is called.
    Accumulated data can be accessed with :meth:`get_data` and :meth:`pop_data`.

    Args:
        name: thread name
        devargs: args supplied to :math:`setup` method
        devkwargs: keyword args supplied to :math:`setup` method
        signal_pool: :class:`.SignalPool` for this thread (by default, use the default common pool)

    Attributes:
        block_period: size of a row block which causes :meth:`on_new_block` call

    Methods to overload:
        :meth:`setup`: set up the thread
        :meth:`cleanup`: clean up the thread 
        :meth:`prepare_row`: modify the new complete row before before adding it to the block
    """
    def __init__(self, name=None, setupargs=None, setupkwargs=None, signal_pool=None):
        controller.QThreadController.__init__(self,name=name,kind="loop",signal_pool=signal_pool)
        self.channels={}
        self.table={}
        self._channel_lock=threading.RLock()
        self._row_lock=threading.RLock()
        self._row_cnt=0
        self._partial_rows=[]
        self.block_period=1
        self._new_block_done.connect(self._on_new_block_slot,type=QtCore.Qt.QueuedConnection)
        self._new_row_done.connect(self._add_new_row,type=QtCore.Qt.QueuedConnection)
        self._new_row_started.connect(self._start_new_row,type=QtCore.Qt.QueuedConnection)
        self.setupargs=setupargs or []
        self.setupkwargs=setupkwargs or {}

    def setup(self):
        """Set up the thread"""
        pass
    def prepare_row(self, row):
        """Prepare the row"""
        return row
    _new_block_done=QtCore.pyqtSignal()
    @controller.exsafeSlot()
    def _on_new_block_slot(self):
        self.on_new_block()
    def on_new_block(self):
        """Gets called every time a new block is complete"""
        pass
    def cleanup(self):
        """Clean up the thread"""
        pass

    def on_start(self):
        controller.QThreadController.on_start(self)
        self.setup(*self.setupargs,**self.setupkwargs)
    def on_finish(self):
        self.cleanup()

    class ChannelQueue(object):
        """
        Queue for a single channel.

        Manages adding and updating new datapoints.
        For arguments, see :meth:`.StreamFormerThread.add_channel`.
        """
        QueueStatus=collections.namedtuple("QueueStatus",["queue_len","enabled"])
        def __init__(self, func=None, max_queue_len=1, required="auto", enabled=True, fill_on="started", latching=True, expand_list=False, default=None):
            object.__init__(self)
            funcargparse.check_parameter_range(fill_on,"fill_on",{"started","completed"})
            self.func=func
            self.queue=collections.deque()
            self.required=(func is None) if required=="auto" else required
            self.max_queue_len=max_queue_len
            self.enabled=enabled
            self.fill_on=fill_on
            self.last_value=default
            self.default=default
            self.latching=latching
            self.expand_list=expand_list
        def add(self, value):
            """
            Add a new value to the queue
            
            Return ``True`` is the value was added and the queue was expanded and ``False`` otherwise.
            """
            if self.expand_list and isinstance(value,list):
                res=None
                for v in value:
                    res=self.add(v)
                return res
            else:
                data_available=bool(self.queue)
                if self.enabled:
                    self.queue.append(value)
                    if self.max_queue_len>0 and len(self.queue)>self.max_queue_len:
                        self.queue.popleft()
                    if self.latching:
                        self.last_value=value
                return self.queue and not (data_available)
        def add_from_func(self):
            """Fill the queue from the function (if available)"""
            if self.enabled and self.func and self.fill_on=="started":
                self.queue.append(self.func())
                return True
            return False
        def queued_len(self):
            """Get queue length"""
            return len(self.queue)
        def ready(self):
            """Check if at leas one datapoint is ready"""
            return (not self.enabled) or (not self.required) or self.queue
        def enable(self, enable=True):
            """Enable or disable the queue"""
            if self.enabled and not enable:
                self.queue.clear()
            self.enabled=enable
        def set_requried(self, required="auto"):
            """Specify if receiving value is required"""
            self.required=(self.func is None) if required=="auto" else required
        def need_completion(self):
            """Check if the queue needs to be completed by a function"""
            return self.enabled and (not self.queue) and (self.func is not None)
        def get(self):
            """Pop the oldest value"""
            if not self.enabled:
                return None
            elif self.queue:
                return self.queue.popleft()
            elif self.func:
                return self.func()
            elif not self.required:
                return self.last_value
            else:
                raise IndexError("no queued data to get")
        def clear(self):
            """Clear the queue"""
            self.queue.clear()
            self.last_value=self.default
        def get_status(self):
            """
            Get the queue status

            Return tuple ``(queued_length, enabled)``
            """
            return self.QueueStatus(len(self.queue),self.enabled)
            

    def add_channel(self, name, func=None, max_queue_len=1, enabled=True, required="auto", fill_on="started", latching=True, expand_list=False, default=None):
        """
        Add a new channel to the queue.

        Args:
            name (str): channel name
            func: function used to get the channel value if no data has been suppled
            max_queue_len (int): maximal queue length
            enabled (bool): determines if the channel is enabled by default (disabled channel always returns ``None``)
            required: determines if the channel is required to receive the value to complete the row;
                by default, ``False`` if `func` is specified and ``True`` otherwise
            fill_on (str): determines when `func` is called to get the channel value;
                can be either ``"started"`` (when the new row is created) or ``"finished"`` (when the new row is complete)
            latching (bool): determines value of non-`required` channel if `func` is not supplied;
                if ``True``, it is equal to the last received values; otherwise, it is default
            expand_list (bool): if ``True`` and the received value is list, assume that it contains several datapoints and add them sequentially
                (note that this would generally required setting `max_queue_len`>1, otherwise only the last received value will show up)
            default: default channel value
        """
        if name in self.channels:
            raise KeyError("channel {} already exists".format(name))
        self.channels[name]=self.ChannelQueue(func,max_queue_len=max_queue_len,required=required,enabled=enabled,
            fill_on=fill_on,latching=latching,expand_list=expand_list,default=default)
        self.table[name]=[]
    def subscribe_source(self, name, srcs, dsts="any", tags=None, parse=None, filt=None):
        """
        Subscribe a source signal to a channels.

        Called automatically for subscribed channels, so it is rarely called explicitly.

        Args:
            name (str): channel name
            srcs(str or [str]): signal source name or list of source names to filter the subscription;
                can be ``"any"`` (any source) or ``"all"`` (only signals specifically having ``"all"`` as a source).
            dsts(str or [str]): signal destination name or list of destination names to filter the subscription;
                can be ``"any"`` (any destination) or ``"all"`` (only source specifically having ``"all"`` as a destination).
            tags: signal tag or list of tags to filter the subscription (any tag by default).
            parse: if not ``None``, specifies a parsing function which takes 3 arguments (`src`, `tag` and `value`)
                and returns a dictionary ``{name: value}`` of channel values to add
                (useful is a single signal contains multiple channel values, e.g., multiple daq channels)
                The function is called in the signal source thread, so it should be quick and non-blocking
            filt(callable): additional filter function which takes 4 arguments: signal source, signal destination, signal tag, signal value,
                and checks whether signal passes the requirements.
        """
        def on_signal(src, tag, value):
            self._add_data(name,value,src=src,tag=tag,parse=parse)
        self.subscribe_nonsync(on_signal,srcs=srcs,dsts=dsts,tags=tags,filt=filt)
    def configure_channel(self, name, enable=True, required="auto", clear=True):
        """
        Reconfigure existing channel.

        Args:
            name (str): channel name
            enabled (bool): determines if the channel is enabled by default (disabled channel always returns ``None``)
            required: determines if the channel is required to receive the value to complete the row;
                by default, ``False`` if `func` is specified and ``True`` otherwise
            clear (bool): if ``True``, clear all channels after reconfiguring
        """
        with self._channel_lock:
            self.channels[name].enable(enable)
            self.channels[name].set_requried(required)
            if clear:
                self.clear_all()
            
    def _add_data(self, name, value, src=None, tag=None, parse=None):
        """
        Add a value to the channel.

        Called automatically for subscribed channels, so it is rarely called explicitly.

        Args:
            name (str): channel name
            value: value to add
            src (str): specifies values source; supplied to the `parse` function
            tag (str): specifies values tag; supplied to the `parse` function
            parse: if not ``None``, specifies a parsing function which takes 3 arguments (`src`, `tag` and `value`)
                and returns a dictionary ``{name: value}`` of channel values to add
                (useful is a single signal contains multiple channel values, e.g., multiple daq channels)
                The function is called in the signal source thread, so it should be quick and non-blocking
        """
        with self._channel_lock:
            _max_queued_before=0
            _max_queued_after=0
            if parse is not None:
                row=parse(src,tag,value)
            else:
                row={name:value}
            for name,value in viewitems(row):
                ch=self.channels[name]
                _max_queued_before=max(_max_queued_before,ch.queued_len())
                self.channels[name].add(value)
                _max_queued_after=max(_max_queued_after,ch.queued_len())
            row_ready=True
            for _,ch in viewitems(self.channels):
                if not ch.ready():
                    row_ready=False
                    break
            if row_ready:
                part_row={}
                for n,ch in viewitems(self.channels):
                    if not ch.need_completion():
                        part_row[n]=ch.get()
                self._partial_rows.append(part_row)
                self._new_row_done.emit()
            elif _max_queued_after>_max_queued_before:
                self._new_row_started.emit()
    _new_row_started=QtCore.pyqtSignal()
    @controller.exsafeSlot()
    def _start_new_row(self):
        _max_queued=0
        with self._channel_lock:
            for _,ch in viewitems(self.channels):
                _max_queued=max(_max_queued,ch.queued_len())
        for _,ch in viewitems(self.channels):
            while ch.queued_len()<_max_queued:
                if not ch.add_from_func():
                    break
    _new_row_done=QtCore.pyqtSignal()
    @controller.exsafeSlot()
    def _add_new_row(self):
        with self._channel_lock:
            if not self._partial_rows: # in case reset was call in the meantime
                return
            row=self._partial_rows.pop(0)
        for n,ch in viewitems(self.channels):
            if n not in row:
                row[n]=ch.get()
        row=self.prepare_row(row)
        with self._row_lock:
            for n,t in viewitems(self.table):
                t.append(row[n])
            self._row_cnt+=1
            if self._row_cnt>=self.block_period:
                self._row_cnt=0
                self._new_block_done.emit()




    def get_data(self, nrows=None, columns=None, copy=True):
        """
        Get accumulated data.

        Args:
            nrows: number of rows to get; by default, all complete rows
            columns: list of channel names to get; by default all channels
            copy (bool): if ``True``, return copy of the internal storage table (otherwise the returned data can increase in size).

        Return dictionary ``{name: [value]}`` of channel value lists (all lists have the same length) if columns are not specified,
        or a 2D numpy array if the columns are specified.
        """
        if columns is None and nrows is None:
            return self.table.copy() if copy else self.table
        with self._row_lock:
            if nrows is None:
                nrows=len(general.any_item(self.table)[1])
            if columns is None:
                return dict((n,v[:nrows]) for n,v in viewitems(self.table))
            else:
                return np.column_stack([self.table[c][:nrows] for c in columns])
    def pop_data(self, nrows=None, columns=None):
        """
        Pop accumulated data.

        Same as :meth:`get_data`, but removes the returned data from the internal storage.
        """
        if nrows is None:
            with self._row_lock:
                table=self.table
                self.table=dict([(n,[]) for n in table])
            if columns is None:
                return dict((n,v) for n,v in viewitems(table))
            else:
                return np.column_stack([table[c] for c in columns])
        with self._row_lock:
            res=self.get_data(nrows=nrows,columns=columns)
            for _,c in viewitems(self.table):
                del c[:nrows]
            return res

    def clear_table(self):
        """Clear table containing all complete rows"""
        with self._row_lock:
            self.table=dict([(n,[]) for n in self.table])
    def clear_all(self):
        """Clear everything: table of complete rows and all channel queues"""
        with self._row_lock, self._channel_lock:
            self.table=dict([(n,[]) for n in self.table])
            for _,ch in viewitems(self.channels):
                ch.clear()
            self._partial_rows=[]

    def get_channel_status(self):
        """
        Get channel status.

        Return dictionary ``{name: status}``, where ``status`` is a tuple ``(queued_length, enabled)``.
        """
        status={}
        with self._channel_lock:
            for n,ch in viewitems(self.channels):
                status[n]=ch.get_status()
        return status





class TableAccumulator(object):
    """
    Data accumulator which receives data chunks and adds them into a common table.

    Can receive either list of columns, or dictionary of named columns; designed to work with :class:`StreamFormerThread`.

    Args:
        channels ([str]): channel names
        memsize(int): maximal number of rows to store
    """
    def __init__(self, channels, memsize=1000000):
        object.__init__(self)
        self.channels=channels
        self.data=[[] for _ in channels]
        self.memsize=memsize

    def add_data(self, data):
        """
        Add new data to the table.

        Data can either be a list of columns, or a dictionary ``{name: [data]}`` with named columns.
        """
        if isinstance(data,dict):
            table_data=[]
            for ch in self.channels:
                if ch not in data:
                    raise KeyError("data doesn't contain channel {}".format(ch))
                table_data.append(data[ch])
            data=table_data
        minlen=min([len(incol) for incol in data])
        for col,incol in zip(self.data,data):
            col.extend(incol[:minlen])
            if len(col)>self.memsize:
                del col[:len(col)-self.memsize]
        return minlen
    def reset_data(self, maxlen=0):
        """Clear all data in the table"""
        for col in self.data:
            del col[:len(col)-maxlen]
    
    def get_data_columns(self, channels=None, maxlen=None):
        """
        Get table data as a list of columns.
        
        Args:
            channels: list of channels to get; all channels by default
            maxlen: maximal column length (if stored length is larger, return last `maxlen` rows)
        """
        channels=channels or self.channels
        data=[]
        for ch in channels:
            col=self.data[self.channels.index(ch)]
            if maxlen is not None:
                start=max(0,len(col)-maxlen)
                col=col[start:]
            data.append(col)
        return data
    def get_data_rows(self, channels=None, maxlen=None):
        """
        Get table data as a list of rows.
        
        Args:
            channels: list of channels to get; all channels by default
            maxlen: maximal column length (if stored length is larger, return last `maxlen` rows)
        """
        return list(zip(*self.get_data_columns(channels=channels,maxlen=maxlen)))
    def get_data_dict(self, channels=None, maxlen=None):
        """
        Get table data as a dictionary ``{name: column}``.
        
        Args:
            channels: list of channels to get; all channels by default
            maxlen: maximal column length (if stored length is larger, return last `maxlen` rows)
        """
        channels=channels or self.channels
        return dict(zip(channels,self.get_data_columns(maxlen=maxlen)))


class TableAccumulatorThread(controller.QTaskThread):
    """
    Table accumulator thread which provides async access to :class:`TableAccumulator` instance.

    Args:
        channels ([str]): channel names
        data_source (str): source thread which emits new data signals (typically, a name of :class:`StreamFormerThread` thread)
        memsize(int): maximal number of rows to store
    """
    def setup_task(self, channels, data_source, memsize=1000000):
        self.channels=channels
        self.fmt=[None]*len(channels)
        self.table_accum=TableAccumulator(channels=channels,memsize=memsize)
        self.subscribe(self._accum_data,srcs=data_source,dsts="any",tags="points",limit_queue=1000)
        self.subscribe(self._on_source_reset,srcs=data_source,dsts="any",tags="reset")
        self.logger=None
        self.streaming=False
        self.add_command("start_streaming",self.start_streaming)
        self.add_command("stop_streaming",self.stop_streaming)
        self.data_lock=threading.Lock()

    def start_streaming(self, path, source_trigger=False, append=False):
        """
        Start streaming data to the disk.

        Args:
            path (str): path to the file
            source_trigger (bool): if ``True``, start streaming only after source ``"reset"`` signal; otherwise, start streaming immediately
            append (bool): if ``True``, append new data to the existing file; otherwise, overwrite the file
        """
        self.streaming=not source_trigger
        if not append and os.path.exists(path):
            file_utils.retry_remove(path)
        self.logger=logfile.LogFile(path)
    def stop_streaming(self):
        """Stop streaming data to the disk"""
        self.logger=None
        self.streaming=False

    
    def _on_source_reset(self, src, tag, value):
        with self.data_lock:
            self.table_accum.reset_data()
        if self.logger and not self.streaming:
            self.streaming=True

    def _accum_data(self, src, tag, value):
        with self.data_lock:
            added_len=self.table_accum.add_data(value)
        if self.logger and self.streaming:
            new_data=self.table_accum.get_data_rows(maxlen=added_len)
            self.logger.write_multi_datalines(new_data,columns=self.channels,add_timestamp=False,fmt=self.fmt)

    def get_data_sync(self, channels=None, maxlen=None, fmt="rows"):
        """
        Get accumulated table data.
        
        Args:
            channels: list of channels to get; all channels by default
            maxlen: maximal column length (if stored length is larger, return last `maxlen` rows)
            fmt (str): return format; can be ``"rows"`` (list of rows), ``"columns"`` (list of columns), or ``"dict"`` (dictionary of named columns)
        """
        with self.data_lock:
            if fmt=="columns":
                return self.table_accum.get_data_columns(channels=channels,maxlen=maxlen)
            elif fmt=="rows":
                return self.table_accum.get_data_rows(channels=channels,maxlen=maxlen)
            elif fmt=="dict":
                return self.table_accum.get_data_dict(channels=channels,maxlen=maxlen)
            else:
                raise ValueError("unrecognized data format: {}".format(fmt))
    def reset(self):
        """Clear all data in the table"""
        with self.data_lock:
            self.table_accum.reset_data()