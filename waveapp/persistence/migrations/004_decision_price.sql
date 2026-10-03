-- 2026-08-24 (option A after the first-Monday fill delay): every
-- position remembers the price Wave DECIDED at, so realized entry slippage
-- (fill vs decision) is measurable per trade — the §11.2 calibration.
ALTER TABLE positions ADD COLUMN decision_price REAL;
