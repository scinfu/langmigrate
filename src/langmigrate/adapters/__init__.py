"""Database-specific bulk adapters for the proactive batch CLI.

Database client imports are confined to this package and loaded lazily so that
importing ``langmigrate`` never requires an optional backend extra to be installed.
"""
