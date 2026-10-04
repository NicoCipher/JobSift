-- Apply after postgres_schema.sql.
-- Kept separate because Neon schema-branch migration parsing does not accept
-- procedural function bodies inside a multi-statement migration payload.

CREATE OR REPLACE FUNCTION jobsift_reject_operator_state_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'operator outcome state is immutable';
END;
$$;

DROP TRIGGER IF EXISTS outcome_events_no_update ON operator_outcome_events;
CREATE TRIGGER outcome_events_no_update
BEFORE UPDATE ON operator_outcome_events
FOR EACH ROW EXECUTE FUNCTION jobsift_reject_operator_state_mutation();

DROP TRIGGER IF EXISTS outcome_events_no_delete ON operator_outcome_events;
CREATE TRIGGER outcome_events_no_delete
BEFORE DELETE ON operator_outcome_events
FOR EACH ROW EXECUTE FUNCTION jobsift_reject_operator_state_mutation();

DROP TRIGGER IF EXISTS outcome_imports_no_update ON operator_outcome_imports;
CREATE TRIGGER outcome_imports_no_update
BEFORE UPDATE ON operator_outcome_imports
FOR EACH ROW EXECUTE FUNCTION jobsift_reject_operator_state_mutation();

DROP TRIGGER IF EXISTS outcome_imports_no_delete ON operator_outcome_imports;
CREATE TRIGGER outcome_imports_no_delete
BEFORE DELETE ON operator_outcome_imports
FOR EACH ROW EXECUTE FUNCTION jobsift_reject_operator_state_mutation();
