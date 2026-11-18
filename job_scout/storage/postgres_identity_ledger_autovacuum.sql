-- Keep the protected identity ledger's dead-tuple backlog small.
-- This does not delete or rewrite ledger rows; it makes ordinary autovacuum
-- eligible at roughly 2% dead tuples (plus the fixed threshold).
ALTER TABLE public.job_identity_ledger SET (
    autovacuum_vacuum_threshold = 500,
    autovacuum_vacuum_scale_factor = 0.02
);
