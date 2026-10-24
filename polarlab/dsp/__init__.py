"""Elaborazione del segnale in streaming.

Ogni processore ha stato interno e un metodo `push(...)` che consuma un blocco di
campioni appena arrivato: nessun ricalcolo dell'intera finestra a ogni campione.
Le metriche su finestra (HRV, DFA, respirazione) si ricalcolano a bassa frequenza (≈1 Hz).
"""
