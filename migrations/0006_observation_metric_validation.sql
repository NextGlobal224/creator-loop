-- Reject invalid legacy measurements; never repair append-only history.
CREATE TABLE _observation_metric_validation (
  invalid_metric_count INTEGER NOT NULL CHECK(invalid_metric_count=0)
);
INSERT INTO _observation_metric_validation
SELECT count(*) FROM observation_metrics WHERE
  length(trim(metric_key))=0 OR metric_key!=trim(metric_key) OR
  length(trim(unit))=0 OR length(trim(definition_version))=0 OR
  typeof(numeric_value) NOT IN ('integer','real') OR
  numeric_value>1.7976931348623157e308 OR numeric_value< -1.7976931348623157e308 OR
  (lower(trim(unit))='count' AND (numeric_value<0 OR numeric_value!=round(numeric_value)));
DROP TABLE _observation_metric_validation;

CREATE TRIGGER observation_metric_validate BEFORE INSERT ON observation_metrics
WHEN length(trim(NEW.metric_key))=0 OR NEW.metric_key!=trim(NEW.metric_key) OR
  length(trim(NEW.unit))=0 OR length(trim(NEW.definition_version))=0 OR
  typeof(NEW.numeric_value) NOT IN ('integer','real') OR
  NEW.numeric_value>1.7976931348623157e308 OR NEW.numeric_value< -1.7976931348623157e308 OR
  (lower(trim(NEW.unit))='count' AND (NEW.numeric_value<0 OR NEW.numeric_value!=round(NEW.numeric_value)))
BEGIN SELECT RAISE(ABORT,'invalid observation metric'); END;
