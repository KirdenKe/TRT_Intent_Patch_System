from trt_core.isaac_startup_timing import fallback_startup_timing, startup_marker_name
from trt_core.m12 import verification_seconds_excluding_startup


def test_startup_markers_accept_variable_environment_ids():
    assert startup_marker_name(
        "[12.0s] Cannot assign transform to non-root articulation link at "
        "'/World/Envs/Env12/ur5/Gripper/robotiq_arg2f_base_link'"
    ) == "NON_ROOT_ARTICULATION_TRANSFORM"
    assert startup_marker_name(
        "[15.0s] Cannot assign velocities to rigid body at "
        "'/World/Envs/Env3/ur5/Gripper/robotiq_arg2f_base_link'"
    ) == "RIGID_BODY_VELOCITY"


def test_internal_timestamp_fallback_uses_latest_matching_startup_structure():
    timing = fallback_startup_timing(
        [
            "[8.25s] Cannot assign transform to non-root articulation link at "
            "'/World/Envs/Env0/ur5/Gripper/robotiq_arg2f_base_link'",
            "[11.50s] Client gpu.foundation.plugin has acquired "
            "[gpu::unstable::IMemoryBudgetManagerFactory v0.1] 100 times. "
            "Consider accessing this interface with carb::getCachedInterface()",
            "[10.00s] Cannot assign velocities to rigid body at "
            "'/World/Envs/Env4/ur5/Gripper/robotiq_arg2f_base_link'",
        ]
    )

    assert timing is not None
    assert timing["isaac_startup_seconds"] == 11.5
    assert timing["startup_reference_pattern"] == "GPU_MEMORY_BUDGET_FACTORY_WARNING"
    assert timing["startup_reference_source"] == "ISAAC_INTERNAL_TIMESTAMP"


def test_verification_time_excludes_measured_isaac_startup():
    adjusted, wall, startup = verification_seconds_excluding_startup(
        "2026-08-06T00:00:00Z",
        "2026-08-06T00:10:00Z",
        180.0,
    )
    assert wall == 600.0
    assert startup == 180.0
    assert adjusted == 420.0


def test_verification_time_is_incomplete_without_startup_boundary():
    adjusted, wall, startup = verification_seconds_excluding_startup(
        "2026-08-06T00:00:00Z",
        "2026-08-06T00:10:00Z",
        None,
    )
    assert wall == 600.0
    assert startup is None
    assert adjusted is None
