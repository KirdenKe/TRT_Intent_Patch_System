from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--num_envs", type=int, required=True)
    parser.add_argument("--headless", choices=["true", "false"], required=True)
    parser.add_argument("--global_seed", type=int)
    parser.add_argument("--max_seed_trials", type=int)
    parser.add_argument("--seed_db_path")
    parser.add_argument("--allowed_overlap_ratio", type=float, required=True)
    parser.add_argument("--layout_source", required=True)
    parser.add_argument("--episode_success_requires_reset_cycles", type=int, required=True)
    parser.add_argument("--chosen_intervention_mode", required=True)
    parser.add_argument("--travel_time", type=float, required=True)
    parser.add_argument("--fix_duration", type=float, required=True)
    parser.add_argument("--resume_delay", type=float, required=True)
    parser.add_argument("--add_reference_number", type=int, required=True)
    parser.add_argument("--reuse_verified_seed", action="store_true")
    parser.add_argument("--reuse_precomputed_layouts", action="store_true")
    args = parser.parse_args()
    print(
        "fake pick_up_example "
        f"num_envs={args.num_envs} "
        f"headless={args.headless} "
        f"global_seed={args.global_seed} "
        f"allowed_overlap_ratio={args.allowed_overlap_ratio} "
        f"layout_source={args.layout_source} "
        f"add_reference_number={args.add_reference_number} "
        f"reuse_verified_seed={args.reuse_verified_seed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
