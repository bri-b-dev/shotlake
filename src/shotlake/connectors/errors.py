class SchemaDriftError(Exception):
    """Quelle liefert ein unerwartetes Schema (fehlende Pflichtspalte, geänderte Einheit)."""
