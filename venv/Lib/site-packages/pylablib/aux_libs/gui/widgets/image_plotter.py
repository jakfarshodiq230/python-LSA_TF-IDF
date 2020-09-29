"""
PyQtGraph-based image plotter.

Has 2 parts: :class:`ImageView` which displays the image,
and :class:`ImageViewController` which controls the image display (value ranges, flipping or transposing, etc.)
:class:`ImageView` can also operate alone without a controller.
When both are used, :class:`ImageView` is created and set up first, and then supplied to :meth:`ImageViewController.setupUi` method.
"""

from .param_table import ParamTable, FixedParamTable
from ....core.gui.qt.thread import controller

from PyQt5 import QtWidgets, QtCore
import pyqtgraph
import numpy as np
import contextlib


class ImageViewController(QtWidgets.QWidget):
    """
    Class for controlling an image inside :class:`ImageView`.

    Like most widgets, requires calling :meth:`setupUi` to set up before usage.

    Args:
        parent: parent widget
    """
    def __init__(self, parent=None):
        super(ImageViewController, self).__init__(parent)

    def setupUi(self, name, view, display_table=None, display_table_root=None):
        """
        Setup the image view controller.

        Args:
            name (str): widget name
            view (ImageView): controlled image view
            display_table (bool): as :class:`.IndicatorValuesTable` object used to access table values; by default, create one internally
            display_table_root (str): if not ``None``, specify root (i.e., path prefix) for values inside the table.
        """
        self.name=name
        self.setObjectName(self.name)
        self.layout=QtWidgets.QHBoxLayout(self)
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setObjectName("layout")
        self.view=view
        self.view.attach_controller(self)
        self.settings_table=ParamTable(self)
        self.settings_table.setObjectName("settings_table")
        self.layout.addWidget(self.settings_table)
        self.img_lim=(0,65536)
        self.settings_table.setupUi("img_settings",add_indicator=True,display_table=display_table,display_table_root=display_table_root)
        self.settings_table.add_text_label("size",label="Image size:")
        self.settings_table.add_check_box("flip_x","Flip X",value=False)
        self.settings_table.add_check_box("flip_y","Flip Y",value=False)
        self.settings_table.add_check_box("transpose","Transpose",value=False)
        self.settings_table.add_check_box("normalize","Normalize",value=False)
        self.settings_table.add_num_edit("minlim",value=self.img_lim[0],limiter=self.img_lim+("coerce","int"),formatter=("int"),label="Minimal intensity:")
        self.settings_table.add_num_edit("maxlim",value=self.img_lim[1],limiter=self.img_lim+("coerce","int"),formatter=("int"),label="Maximal intensity:")
        self.settings_table.add_check_box("show_lines","Show lines",value=True)
        self.settings_table.add_num_edit("vlinepos",value=0,limiter=(0,None,"coerce","float"),formatter=("float","auto",1,True),label="X line:")
        self.settings_table.add_num_edit("hlinepos",value=0,limiter=(0,None,"coerce","float"),formatter=("float","auto",1,True),label="Y line:")
        self.settings_table.add_button("center_lines","Center lines").value_changed_signal().connect(view.center_lines)
        self.settings_table.value_changed.connect(lambda: self.view.update_image(update_controls=False))
        self.settings_table.add_spacer(10)
        self.settings_table.add_button("update_image","Updating",checkable=True)
        self.settings_table.add_button("single","Single").value_changed_signal().connect(self.view.arm_single)
        self.settings_table.add_padding()

    def set_img_lim(self, *args):
        """
        Set up image limits

        Can specify either only upper limit (lower stays the same), or both limits.
        """
        if len(args)==1:
            self.img_lim=(self.img_lim[0],args[0])
        elif len(args)==2:
            self.img_lim=tuple(args)
        else:
            return
        minl,maxl=self.img_lim
        self.settings_table.w["minlim"].set_number_limit(minl,maxl,"coerce","int")
        self.settings_table.w["maxlim"].set_number_limit(minl,maxl,"coerce","int")
    def get_all_values(self):
        """Get all control values"""
        return self.settings_table.get_all_values()
    def set_all_values(self, params):
        """Set all control values"""
        self.settings_table.set_all_values(params)


builtin_cmaps={ "gray":([0,1.],[(0.,0.,0.),(1.,1.,1.)]),
                "gray_sat":([0,0.999,1.],[(0.,0.,0.),(1.,1.,1.),(1.,0.,0.)]),
                "hot":([0,0.3,0.7,1.],[(0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(1.,1.,1.)]),
                "hot_sat":([0,0.3,0.7,0.999,1.],[(0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(1.,1.,1.),(0.,0.,1.)])
            }
class ImageView(QtWidgets.QWidget):
    """
    Image view object.

    Built on top of :class:`pyqtgraph.ImageView` class.

    Args:
        parent: parent widget
    """
    def __init__(self, parent=None):
        super(ImageView, self).__init__(parent)
        self.ctl=None

    class Rectangle(object):
        def __init__(self, rect, center=None, size=None):
            object.__init__(self)
            self.rect=rect
            self.center=center or (0,0)
            self.size=size or (0,0)
        def update_params(self, center=None, size=None):
            if center:
                self.center=center
            if size:
                self.size=size
    def setupUi(self, name, img_size=(1024,1024), min_size=(512,512)):
        """
        Setup the image view.

        Args:
            name (str): widget name
            img_size (tuple): default image size (used only until actual image is supplied)
            min_size (tuple): minimal widget size
        """
        self.name=name
        self.setObjectName(self.name)
        self.single=False
        self.layout=QtWidgets.QHBoxLayout(self)
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setObjectName("layout")
        self.img=np.zeros(img_size)
        self.imageWindow=pyqtgraph.ImageView(self)
        if min_size:
            self.imageWindow.setMinimumSize(QtCore.QSize(*min_size))
        self.imageWindow.setObjectName("imageWindow")
        self.layout.addWidget(self.imageWindow)
        self.set_colormap("hot_sat")
        self.imageWindow.ui.roiBtn.hide()
        self.imageWindow.ui.menuBtn.hide()
        self.imgVLine=pyqtgraph.InfiniteLine(angle=90,movable=True,bounds=[0,None])
        self.imgHLine=pyqtgraph.InfiniteLine(angle=0,movable=True,bounds=[0,None])
        self.imageWindow.getView().addItem(self.imgVLine)
        self.imageWindow.getView().addItem(self.imgHLine)
        self._signals_connected=False
        self._connect_signals()
        self.imgVLine.sigPositionChanged.connect(self.update_image_controls)
        self.imgHLine.sigPositionChanged.connect(self.update_image_controls)
        self.imageWindow.getHistogramWidget().sigLevelsChanged.connect(self.update_image_controls)
        self.rectangles={}

    def attach_controller(self, ctl):
        """
        Attach :class:`ImageViewController` object.

        Called automatically in :meth:`ImageViewController.setupUi`
        """
        self.ctl=ctl
    def _get_params(self):
        if self.ctl is not None:
            return self.ctl.settings_table
        return FixedParamTable(v={"transpose":False,
                "flip_x":False,
                "flip_y":False,
                "normalize":True,
                "show_lines":False,
                "vlinepos":0,
                "hlinepos":0,
                "update_image":True})

    def set_colormap(self, cmap):
        """
        Setup colormap.

        Can be name of one built-in colormaps (``"gray"``, ``"gray_sat"``, ``"hot"``, ``"hot_sat"``),
        a list specifying PyQtGraph colormap or a :class:`pyqtgraph.ColorMap` instance.
        """
        cmap=builtin_cmaps.get(cmap,cmap)
        if isinstance(cmap,(list,tuple)):
            cmap=pyqtgraph.ColorMap(*cmap)
        self.imageWindow.setColorMap(cmap)
    
    def _connect_signals(self):
        if not self._signals_connected:
            self.imgVLine.sigPositionChanged.connect(self.update_image_controls)
            self.imgHLine.sigPositionChanged.connect(self.update_image_controls)
            self.imageWindow.getHistogramWidget().sigLevelsChanged.connect(self.update_image_controls)
            self._signals_connected=True
    def _disconnect_signals(self):
        if self._signals_connected:
            self.imgVLine.sigPositionChanged.disconnect(self.update_image_controls)
            self.imgHLine.sigPositionChanged.disconnect(self.update_image_controls)
            self.imageWindow.getHistogramWidget().sigLevelsChanged.disconnect(self.update_image_controls)
            self._signals_connected=False
    @contextlib.contextmanager
    def _no_events(self):
        self._disconnect_signals()
        try:
            yield
        finally:
            self._connect_signals()


    def set_image(self, img):
        """
        Set the current image.

        The image display won't be updated until :meth:`update_image` is called
        """
        self.img=img
    @controller.exsafe
    def center_lines(self):
        """Center coordinate lines"""
        imshape=self.img.shape[::-1] if self._get_params().v["transpose"] else self.img.shape
        self.imgVLine.setPos(imshape[0]/2)
        self.imgHLine.setPos(imshape[1]/2)
    def arm_single(self):
        """Arm the single-image trigger"""
        self.single=True
    def set_rectangle(self, name, center=None, size=None):
        """
        Add or change parameters of a rectangle with a given name.

        Rectangle coordinates are specified in the original image coordinate system
        (i.e., rectangles are automatically flipped/transposed with the image).
        """
        if name not in self.rectangles:
            pqrect=pyqtgraph.ROI((0,0),(0,0),movable=False)
            self.imageWindow.getView().addItem(pqrect)
            self.rectangles[name]=self.Rectangle(pqrect)
        rect=self.rectangles[name]
        rect.update_params(center,size)
        rcenter=rect.center[0]-rect.size[0]/2.,rect.center[1]-rect.size[1]/2.
        rsize=rect.size
        imshape=self.img.shape
        params=self._get_params()
        if params.v["transpose"]:
            rcenter=rcenter[::-1]
            rsize=rsize[::-1]
            imshape=imshape[::-1]
        if params.v["flip_x"]:
            rcenter=(imshape[0]-rcenter[0]-rsize[0]),rcenter[1]
        if params.v["flip_y"]:
            rcenter=rcenter[0],(imshape[1]-rcenter[1]-rsize[1])
        rect.rect.setPos(rcenter)
        rect.rect.setSize(rsize)
    def update_rectangles(self):
        """Update rectangle coordinates"""
        for name in self.rectangles:
            self.set_rectangle(name)
    def del_rectangle(self, name):
        """Delete a rectangle with a given name"""
        if name in self.rectangles:
            rect=self.rectangles.pop(name)
            self.imageWindow.getView().removeItem(rect)
    def show_rectangles(self, show=True):
        """Toggle showing rectangles on or off"""
        imgview=self.imageWindow.getView()
        for rect in self.rectangles.values():
            if show and rect.rect not in imgview.addedItems:
                imgview.addItem(rect.rect)
            if (not show) and rect.rect in imgview.addedItems:
                imgview.removeItem(rect.rect)
    # Update image controls based on PyQtGraph image window
    @controller.exsafeSlot()
    def update_image_controls(self):
        """Update image controls in the connected :class:`ImageViewController` object"""
        params=self._get_params()
        levels=self.imageWindow.getHistogramWidget().getLevels()
        params.v["minlim"],params.v["maxlim"]=levels
        params.v["vlinepos"]=self.imgVLine.getPos()[0]
        params.v["hlinepos"]=self.imgHLine.getPos()[1]
    def _sanitize_img(self, img, targetImageSize=20): # PyQtGraph histogram has unfortunate failure modes
        steps=(int(np.ceil(img.shape[0] / targetImageSize)), int(np.ceil(img.shape[1] / targetImageSize)))
        step_img=img[::steps[0],::steps[1]] # ImageView only uses stepped image for plotting
        if np.all(step_img==step_img[0,0]): # ImageView can't plot images of constant color
            img=img.copy()
            if img[0,0]==0:
                img[0,0]+=1
            else:
                img[0,0]-=1
        return img
    # Update image plot
    @controller.exsafe
    def update_image(self, update_controls=False):
        """
        Update displayed image.

        If ``update_controls==True``, update control values (such as image min/max values and line positions).
        """
        with self._no_events():
            params=self._get_params()
            if not (params.v["update_image"] or self.single):
                return params
            self.single=False
            draw_img=self.img
            if params.v["transpose"]:
                draw_img=draw_img.transpose()
            if params.v["flip_x"]:
                draw_img=draw_img[::-1,:]
            if params.v["flip_y"]:
                draw_img=draw_img[:,::-1]
            img_shape=draw_img.shape
            if np.prod(img_shape)<=1: # ImageView can't plot images with less than 1 px
                draw_img=np.zeros((2,2),dtype=np.asarray(draw_img).dtype)+(draw_img[0,0] if np.prod(draw_img.shape) else 0)
            autoscale=params.v["normalize"]
            draw_img=self._sanitize_img(draw_img)
            if self.isVisible():
                self.imageWindow.setImage(draw_img,autoLevels=autoscale,autoHistogramRange=autoscale)
            if update_controls:
                self.update_image_controls()
            if not autoscale:
                levels=params.v["minlim"],params.v["maxlim"]
                self.imageWindow.setLevels(*levels)
                self.imageWindow.getHistogramWidget().setLevels(*levels)
                self.imageWindow.getHistogramWidget().autoHistogramRange()
            params.i["minlim"]=self.imageWindow.levelMin
            params.i["maxlim"]=self.imageWindow.levelMax
            params.v["size"]="{} x {}".format(*img_shape)
            show_lines=params.v["show_lines"]
            for ln in [self.imgVLine,self.imgHLine]:
                ln.setPen("g" if show_lines else None)
                ln.setHoverPen("y" if show_lines else None)
                ln.setMovable(show_lines)
            self.imgVLine.setBounds([0,draw_img.shape[0]])
            self.imgHLine.setBounds([0,draw_img.shape[1]])
            self.imgVLine.setPos(params.v["vlinepos"])
            self.imgHLine.setPos(params.v["hlinepos"])
            self.update_rectangles()
            return params