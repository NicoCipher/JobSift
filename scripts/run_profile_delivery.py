"""Entrypoint applying profile-scoped qualification without changing frozen core code."""
from __future__ import annotations

from job_scout import sourcing_plan
from strict_profile_matcher import match_profile_job

# evaluate_recent_inventory resolves this module global at call time. The generic
# matcher and all frozen job_scout files remain untouched.
sourcing_plan.match_job = match_profile_job

from job_scout.profile_delivery_runner import main


if __name__ == "__main__":
    main()
