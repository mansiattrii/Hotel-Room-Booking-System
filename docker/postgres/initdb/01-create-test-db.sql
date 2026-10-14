-- Runs once, when the data volume is first initialised.
-- pytest and the concurrency experiment use this database so they never touch dev data.
CREATE DATABASE hotel_booking_test;
