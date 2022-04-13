from fv3core._config import DynamicalCoreConfig
from pace.stencils.testing.translate import TranslateFortranData2Py


class TranslateDycoreFortranData2Py(TranslateFortranData2Py):
    def __init__(self, grid, namelist, stencil_factory):
        super().__init__(grid, stencil_factory)
        self.namelist = DynamicalCoreConfig.from_namelist(namelist)