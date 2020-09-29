"""
Universal interface for inter-process communication.

Focus on higher throughput for large numpy arrays via shared memory.
"""

from multiprocessing import Array, Pipe
import ctypes
import collections
import numpy as np


class IIPCChannel(object):
    """
    Generic IPC channel interface
    """
    def send(self, data):
        """Send data"""
        pass
    def recv(self, timeout=None):
        """Receive data"""
        pass
    
    def send_numpy(self, data):
        """Send numpy array"""
        return self.send(data)
    def recv_numpy(self, timeout=None):
        """Receive numpy array"""
        return self.recv(timeout=timeout)

    def get_peer_args(self):
        """Get arguments required to create a peer connection"""
        return ()
    @classmethod
    def from_args(cls, *args):
        """Create a peer connection from teh supplied arguments"""
        return cls(*args)


TPipeMsg=collections.namedtuple("TPipeMsg",["id","data"])
_simple_msg=0
_ack_msg=1
_sharedmem_start=16
_sharedmem_recvd=18
class PipeIPCChannel(IIPCChannel):
    """
    Generic IPC channel interface using pipe.
    """
    def __init__(self, pipe_conn=None):
        IIPCChannel.__init__(self)
        self.conn,self.peer_conn=pipe_conn or Pipe()
    
    def get_peer_args(self):
        """Get arguments required to create a peer connection"""
        return ((self.peer_conn,self.conn),)
    
    def _recv_with_timeout(self, timeout):
        if (timeout is None) or self.conn.poll(timeout):
            return self.conn.recv()
        else:
            raise TimeoutError
    def send(self, data):
        """Send data"""
        self.conn.send(data)
    def recv(self, timeout=None):
        """Receive data"""
        return self._recv_with_timeout(timeout)


class SharedMemIPCChannel(PipeIPCChannel):
    """
    Generic IPC channel interface using pipe and shared memory for large arrays.
    """
    _default_array_size=2**24
    def __init__(self, pipe_conn=None, arr=None, arr_size=None):
        PipeIPCChannel.__init__(self,pipe_conn)
        if arr is None:
            self.arr_size=arr_size or self._default_array_size
            self.arr=Array("c",self.arr_size)
        else:
            self.arr=arr
            self.arr_size=len(arr)
    
    def get_peer_args(self):
        """Get arguments required to create a peer connection"""
        return ((self.peer_conn,self.conn),self.arr,self.arr_size)
    
    def send_numpy(self, data, method="auto"):
        """Send numpy array"""
        if method=="auto":
            method="pipe" if data.nbytes<2**16 else "shm"
        if method=="pipe":
            return PipeIPCChannel.send_numpy(self,data)
        buff_ptr,count=data.ctypes.data,data.nbytes
        self.conn.send(TPipeMsg(_sharedmem_start,(count,data.dtype.str,data.shape)))
        while count>0:
            chunk_size=min(count,self.arr_size)
            ctypes.memmove(ctypes.addressof(self.arr.get_obj()),buff_ptr,chunk_size)
            count-=chunk_size
            buff_ptr+=chunk_size
            self.conn.send(chunk_size)
            self.conn.recv()
    def recv_numpy(self, timeout=None):
        """Receive numpy array"""
        msg=self._recv_with_timeout(timeout)
        if not isinstance(msg,TPipeMsg):
            return msg
        if msg.id==_simple_msg:
            return msg.data
        else:
            count,dtype,shape=msg.data
            buffer=ctypes.create_string_buffer(count)
            buff_ptr=ctypes.addressof(buffer)
            while count>0:
                chunk_size=self._recv_with_timeout(timeout)
                ctypes.memmove(buff_ptr,ctypes.addressof(self.arr.get_obj()),chunk_size)
                buff_ptr+=chunk_size
                count-=chunk_size
                self.conn.send(TPipeMsg(_sharedmem_recvd,None))
            return np.frombuffer(buffer,dtype).reshape(shape)