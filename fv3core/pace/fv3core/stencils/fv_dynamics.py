import logging
from datetime import timedelta
from typing import Dict, Mapping, Optional

from dace.frontend.python.interface import nounroll as dace_no_unroll
from gt4py.gtscript import PARALLEL, computation, interval, log

import pace.dsl.gt4py_utils as utils
import pace.fv3core.stencils.moist_cv as moist_cv
import pace.util
import pace.util.constants as constants
from pace.dsl.dace.orchestration import dace_inhibitor, orchestrate
from pace.dsl.dace.wrapped_halo_exchange import WrappedHaloUpdater
from pace.dsl.stencil import StencilFactory
from pace.dsl.typing import FloatField, FloatFieldK
from pace.fv3core._config import DynamicalCoreConfig
from pace.fv3core.initialization.dycore_state import DycoreState
from pace.fv3core.stencils import fvtp2d, tracer_2d_1l
from pace.fv3core.stencils.basic_operations import copy_defn
from pace.fv3core.stencils.del2cubed import HyperdiffusionDamping
from pace.fv3core.stencils.dyn_core import AcousticDynamics
from pace.fv3core.stencils.neg_adj3 import AdjustNegativeTracerMixingRatio
from pace.fv3core.stencils.remapping import LagrangianToEulerian
from pace.stencils.c2l_ord import CubedToLatLon
from pace.util import Timer
from pace.util.grid import DampingCoefficients, GridData
from pace.util.mpi import MPI
from pace.util.quantity import Quantity


logger = logging.getLogger(__name__)

# nq is actually given by ncnst - pnats, where those are given in atmosphere.F90 by:
# ncnst = Atm(mytile)%ncnst
# pnats = Atm(mytile)%flagstruct%pnats
# here we hard-coded it because 8 is the only supported value, refactor this later!
NQ = 8  # state.nq_tot - spec.namelist.dnats


def pt_adjust(pkz: FloatField, dp1: FloatField, q_con: FloatField, pt: FloatField):
    """
    Args:
        pkz (in):
        dp1 (in):
        q_con (in):
        pt (out):
    """
    with computation(PARALLEL), interval(...):
        pt = pt * (1.0 + dp1) * (1.0 - q_con) / pkz


def set_omega(delp: FloatField, delz: FloatField, w: FloatField, omga: FloatField):
    """
    Args:
        delp (in):
        delz (in):
        w (in):
        omga (out):
    """
    with computation(PARALLEL), interval(...):
        omga = delp / delz * w


def init_pfull(
    ak: FloatFieldK,
    bk: FloatFieldK,
    pfull: FloatField,
    p_ref: float,
):
    with computation(PARALLEL), interval(...):
        ph1 = ak + bk * p_ref
        ph2 = ak[1] + bk[1] * p_ref
        pfull = (ph2 - ph1) / log(ph2 / ph1)


def fvdyn_temporaries(
    quantity_factory: pace.util.QuantityFactory,
) -> Mapping[str, Quantity]:
    tmps = {}
    for name in ["te_2d", "te0_2d", "wsd"]:
        quantity = quantity_factory.zeros(
            dims=[pace.util.X_DIM, pace.util.Y_DIM], units="unknown"
        )
        tmps[name] = quantity
    for name in ["dp1", "cvm"]:
        quantity = quantity_factory.zeros(
            dims=[pace.util.X_DIM, pace.util.Y_DIM, pace.util.Z_DIM],
            units="unknown",
        )
        tmps[name] = quantity
    return tmps


@dace_inhibitor
def log_on_rank_0(msg: str):
    """Print when rank is 0 - outside of DaCe critical path"""
    if not MPI or MPI.COMM_WORLD.Get_rank() == 0:
        logger.info(msg)


class DynamicalCore:
    """
    Corresponds to fv_dynamics in original Fortran sources.
    """

    def __init__(
        self,
        comm: pace.util.CubedSphereCommunicator,
        grid_data: GridData,
        stencil_factory: StencilFactory,
        damping_coefficients: DampingCoefficients,
        config: DynamicalCoreConfig,
        phis: pace.util.Quantity,
        state: DycoreState,
        timestep: timedelta,
        checkpointer: Optional[pace.util.Checkpointer] = None,
    ):
        """
        Args:
            comm: object for cubed sphere inter-process communication
            grid_data: metric terms defining the model grid
            stencil_factory: creates stencils
            damping_coefficients: damping configuration/constants
            config: configuration of dynamical core, for example as would be set by
                the namelist in the Fortran model
            phis: surface geopotential height
            state: model state
            timestep: model timestep
            checkpointer: if given, used to perform operations on model data
                at specific points in model execution, such as testing against
                reference data
        """
        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="step_dynamics",
            dace_compiletime_args=["state", "timer"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="compute_preamble",
            dace_compiletime_args=["state", "is_root_rank"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="_compute",
            dace_compiletime_args=["state", "timer"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="_dyn",
            dace_compiletime_args=["state", "tracers", "timer"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="post_remap",
            dace_compiletime_args=["state", "is_root_rank"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="wrapup",
            dace_compiletime_args=["state", "is_root_rank"],
        )

        orchestrate(
            obj=self,
            config=stencil_factory.config.dace_config,
            method_to_orchestrate="_checkpoint_fvdynamics",
            dace_compiletime_args=["state", "tag"],
        )

        # nested and stretched_grid are options in the Fortran code which we
        # have not implemented, so they are hard-coded here.
        self.call_checkpointer = checkpointer is not None
        if not self.call_checkpointer:
            self.checkpointer = pace.util.NullCheckpointer()
        else:
            self.checkpointer = checkpointer
        nested = False
        stretched_grid = False
        grid_indexing = stencil_factory.grid_indexing
        sizer = pace.util.SubtileGridSizer.from_tile_params(
            nx_tile=config.npx - 1,
            ny_tile=config.npy - 1,
            nz=config.npz,
            n_halo=grid_indexing.n_halo,
            layout=config.layout,
            tile_partitioner=comm.tile.partitioner,
            tile_rank=comm.tile.rank,
            extra_dim_lengths={},
        )
        quantity_factory = pace.util.QuantityFactory.from_backend(
            sizer, backend=stencil_factory.backend
        )
        assert config.moist_phys, "fvsetup is only implemented for moist_phys=true"
        assert config.nwat == 6, "Only nwat=6 has been implemented and tested"
        self.comm_rank = comm.rank
        self.grid_data = grid_data
        self.grid_indexing = grid_indexing
        self._da_min = damping_coefficients.da_min
        self.config = config

        tracer_transport = fvtp2d.FiniteVolumeTransport(
            stencil_factory=stencil_factory,
            grid_data=grid_data,
            damping_coefficients=damping_coefficients,
            grid_type=config.grid_type,
            hord=config.hord_tr,
        )

        self.tracers = {}
        for name in utils.tracer_variables[0:NQ]:
            self.tracers[name] = state.__dict__[name]
        self.tracer_storages = {
            name: quantity.storage for name, quantity in self.tracers.items()
        }

        temporaries = fvdyn_temporaries(quantity_factory)
        self._te_2d = temporaries["te_2d"]
        self._te0_2d = temporaries["te0_2d"]
        self._wsd = temporaries["wsd"]
        self._dp1 = temporaries["dp1"]
        self._cvm = temporaries["cvm"]

        # Build advection stencils
        self.tracer_advection = tracer_2d_1l.TracerAdvection(
            stencil_factory, tracer_transport, self.grid_data, comm, self.tracers
        )
        self._ak = grid_data.ak
        self._bk = grid_data.bk
        self._phis = phis
        self._ptop = self.grid_data.ptop
        pfull_stencil = stencil_factory.from_origin_domain(
            init_pfull, origin=(0, 0, 0), domain=(1, 1, grid_indexing.domain[2])
        )
        pfull = utils.make_storage_from_shape(
            (1, 1, self._ak.shape[0]), backend=stencil_factory.backend
        )
        pfull_stencil(self._ak, self._bk, pfull, self.config.p_ref)
        # workaround because cannot write to FieldK storage in stencil
        self._pfull = utils.make_storage_data(
            pfull[0, 0, :], self._ak.shape, (0,), backend=stencil_factory.backend
        )
        self._fv_setup_stencil = stencil_factory.from_origin_domain(
            moist_cv.fv_setup,
            externals={
                "nwat": self.config.nwat,
                "moist_phys": self.config.moist_phys,
            },
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.domain_compute(),
        )
        self._pt_adjust_stencil = stencil_factory.from_origin_domain(
            pt_adjust,
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.domain_compute(),
        )
        self._set_omega_stencil = stencil_factory.from_origin_domain(
            set_omega,
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.domain_compute(),
        )
        self._copy_stencil = stencil_factory.from_origin_domain(
            copy_defn,
            origin=grid_indexing.origin_full(),
            domain=grid_indexing.domain_full(),
        )
        self.acoustic_dynamics = AcousticDynamics(
            comm,
            stencil_factory,
            grid_data,
            damping_coefficients,
            config.grid_type,
            nested,
            stretched_grid,
            self.config.acoustic_dynamics,
            self._pfull,
            self._phis,
            self._wsd.storage,
            state,
            checkpointer=checkpointer,
        )
        self._hyperdiffusion = HyperdiffusionDamping(
            stencil_factory,
            damping_coefficients,
            grid_data.rarea,
            self.config.nf_omega,
        )
        self._cubed_to_latlon = CubedToLatLon(
            state, stencil_factory, grid_data, config.c2l_ord, comm
        )
        self._cappa = self.acoustic_dynamics.cappa

        if not (not self.config.inline_q and NQ != 0):
            raise NotImplementedError("tracer_2d not implemented, turn on z_tracer")
        self._adjust_tracer_mixing_ratio = AdjustNegativeTracerMixingRatio(
            stencil_factory,
            self.config.check_negative,
            self.config.hydrostatic,
        )

        self._lagrangian_to_eulerian_obj = LagrangianToEulerian(
            stencil_factory,
            config.remapping,
            grid_data.area_64,
            NQ,
            self._pfull,
            tracers=self.tracers,
        )

        full_xyz_spec = grid_indexing.get_quantity_halo_spec(
            grid_indexing.domain_full(add=(1, 1, 1)),
            grid_indexing.origin_compute(),
            dims=[pace.util.X_DIM, pace.util.Y_DIM, pace.util.Z_DIM],
            n_halo=utils.halo,
            backend=stencil_factory.backend,
        )
        self._omega_halo_updater = WrappedHaloUpdater(
            comm.get_scalar_halo_updater([full_xyz_spec]), state, ["omga"], comm=comm
        )
        self._n_split = config.n_split
        self._k_split = config.k_split
        self._conserve_total_energy = config.consv_te
        self._timestep = timestep.total_seconds()

    # See divergence_damping.py, _get_da_min for explanation of this function
    @dace_inhibitor
    def _get_da_min(self) -> float:
        return self._da_min

    def _checkpoint_fvdynamics(self, state: DycoreState, tag: str):
        if self.call_checkpointer:
            self.checkpointer(
                f"FVDynamics-{tag}",
                u=state.u,
                v=state.v,
                w=state.w,
                delz=state.delz,
                ua=state.ua,
                va=state.va,
                uc=state.uc,
                vc=state.vc,
                qvapor=state.qvapor,
            )

    def step_dynamics(
        self,
        state: DycoreState,
        timer: Timer = pace.util.NullTimer(),
    ):
        """
        Step the model state forward by one timestep.

        Args:
            timer: keep time of model sections
            state: model prognostic state and inputs
        """
        self._checkpoint_fvdynamics(state=state, tag="In")
        self._compute(state, timer)
        self._checkpoint_fvdynamics(state=state, tag="Out")

    def compute_preamble(self, state: DycoreState, is_root_rank: bool):
        if self.config.hydrostatic:
            raise NotImplementedError("Hydrostatic is not implemented")
        if __debug__:
            log_on_rank_0("FV Setup")
        self._fv_setup_stencil(
            state.qvapor,
            state.qliquid,
            state.qrain,
            state.qsnow,
            state.qice,
            state.qgraupel,
            state.q_con,
            self._cvm,
            state.pkz,
            state.pt,
            self._cappa,
            state.delp,
            state.delz,
            self._dp1,
        )

        if self._conserve_total_energy > 0:
            raise NotImplementedError("compute total energy is not implemented")

        if (not self.config.rf_fast) and self.config.tau != 0:
            raise NotImplementedError(
                "Rayleigh_Super, called when rf_fast=False and tau !=0"
            )

        if self.config.adiabatic and self.config.kord_tm > 0:
            raise NotImplementedError(
                "unimplemented namelist options adiabatic with positive kord_tm"
            )
        else:
            if __debug__:
                log_on_rank_0("Adjust pt")
            self._pt_adjust_stencil(
                state.pkz,
                self._dp1,
                state.q_con,
                state.pt,
            )

    def __call__(self, *args, **kwargs):
        return self.step_dynamics(*args, **kwargs)

    def _compute(self, state: DycoreState, timer: pace.util.Timer):
        last_step = False
        self.compute_preamble(
            state,
            is_root_rank=self.comm_rank == 0,
        )

        for k_split in dace_no_unroll(range(self._k_split)):
            n_map = k_split + 1
            last_step = k_split == self._k_split - 1
            self._dyn(
                state=state,
                tracers=self.tracers,
                n_map=n_map,
                timer=timer,
                timestep=self._timestep / self._k_split,
            )

            if self.grid_indexing.domain[2] > 4:
                # nq is actually given by ncnst - pnats,
                # where those are given in atmosphere.F90 by:
                # ncnst = Atm(mytile)%ncnst
                # pnats = Atm(mytile)%flagstruct%pnats
                # here we hard-coded it because 8 is the only supported value,
                # refactor this later!

                # do_omega = self.namelist.hydrostatic and last_step
                # TODO: Determine a better way to do this, polymorphic fields perhaps?
                # issue is that set_val in map_single expects a 3D field for the
                # "surface" array
                if __debug__:
                    log_on_rank_0("Remapping")
                with timer.clock("Remapping"):
                    self._lagrangian_to_eulerian_obj(
                        self.tracer_storages,
                        state.pt,
                        state.delp,
                        state.delz,
                        state.peln,
                        state.u,
                        state.v,
                        state.w,
                        state.ua,
                        state.va,
                        self._cappa,
                        state.q_con,
                        state.qcld,
                        state.pkz,
                        state.pk,
                        state.pe,
                        state.phis,
                        self._te0_2d,
                        state.ps,
                        self._wsd,
                        state.omga,
                        self._ak,
                        self._bk,
                        self._pfull,
                        self._dp1,
                        self._ptop,
                        constants.KAPPA,
                        constants.ZVIR,
                        last_step,
                        self._conserve_total_energy,
                        self._timestep / self._k_split,
                        self._timestep,
                    )
                if last_step:
                    da_min: float = self._get_da_min()
                    self.post_remap(
                        state,
                        is_root_rank=self.comm_rank == 0,
                        da_min=da_min,
                    )
        self.wrapup(
            state,
            is_root_rank=self.comm_rank == 0,
        )

    def _dyn(
        self,
        state: DycoreState,
        tracers: Dict[str, Quantity],
        n_map,
        timestep: float,  # time to step forward by
        timer: pace.util.Timer,
    ):
        self._copy_stencil(
            state.delp,
            self._dp1,
        )
        if __debug__:
            log_on_rank_0("DynCore")
        with timer.clock("DynCore"):
            self.acoustic_dynamics(
                state,
                timestep=timestep,
                n_map=n_map,
            )
        if self.config.z_tracer:
            if __debug__:
                log_on_rank_0("TracerAdvection")
            with timer.clock("TracerAdvection"):
                self.tracer_advection(
                    tracers,
                    self._dp1,
                    state.mfxd,
                    state.mfyd,
                    state.cxd,
                    state.cyd,
                    self._timestep / self._k_split,
                )

    def post_remap(
        self,
        state: DycoreState,
        is_root_rank: bool,
        da_min: float,
    ):
        if not self.config.hydrostatic:
            if __debug__:
                log_on_rank_0("Omega")
            self._set_omega_stencil(
                state.delp,
                state.delz,
                state.w,
                state.omga,
            )
        if self.config.nf_omega > 0:
            if __debug__:
                log_on_rank_0("Del2Cubed")
            self._omega_halo_updater.update()
            self._hyperdiffusion(state.omga, 0.18 * da_min)

    def wrapup(
        self,
        state: DycoreState,
        is_root_rank: bool,
    ):
        if __debug__:
            log_on_rank_0("Neg Adj 3")
        self._adjust_tracer_mixing_ratio(
            state.qvapor,
            state.qliquid,
            state.qrain,
            state.qsnow,
            state.qice,
            state.qgraupel,
            state.qcld,
            state.pt,
            state.delp,
            state.delz,
            state.peln,
        )

        if __debug__:
            log_on_rank_0("CubedToLatLon")
        self._cubed_to_latlon(
            state.u,
            state.v,
            state.ua,
            state.va,
        )
