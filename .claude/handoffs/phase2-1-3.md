# Handoff — Phase 2.1 + 2.3

## Fichiers créés

| Fichier | Description |
|---------|-------------|
| `/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/base.py` | Classe abstraite `AiguilleurBase` (ABC) avec `receive`, `send`, `format_for_channel`, `start`, `stop` |
| `/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/__init__.py` | Exports `AiguilleurBase` |
| `/Users/benjaminmarchand/IdeaProjects/relais/portail/reply_policy.py` | Classe `ReplyPolicy` — chargement `reply_policy.yaml` avec fallback, méthodes `should_reply`, `get_policy`, `reload` |
| `/Users/benjaminmarchand/IdeaProjects/relais/portail/prompt_loader.py` | Fonctions `load_prompt(name)` et `list_prompts()` — cache mtime, fallback repo |

## Tests à écrire

### aiguilleur/base.py

```
tests/unit/aiguilleur/test_base.py
```

- `test_cannot_instantiate_abstract` — vérifie que `AiguilleurBase()` lève `TypeError`
- `test_concrete_subclass_requires_all_abstractmethods` — instanciation incomplète → `TypeError`
- `test_concrete_subclass_start_stop_noop` — `start()` et `stop()` par défaut ne lèvent pas
- `test_channel_name_default_empty` — `channel_name == ""`

### portail/reply_policy.py

```
tests/unit/portail/test_reply_policy.py
```

- `test_no_file_returns_true` — aucun fichier → `should_reply` retourne `True`
- `test_enabled_false_blocks_all` — `enabled: false` → `False` pour tout envelope
- `test_channel_whitelist_allows` — channel dans la liste → `True`
- `test_channel_whitelist_blocks` — channel hors liste → `False`
- `test_blocked_user_returns_false` — sender_id dans `blocked_users` → `False`
- `test_get_policy_returns_copy` — modification du retour n'affecte pas l'état interne
- `test_reload_picks_up_changes` — modification du fichier + `reload()` → nouvelle politique

### portail/prompt_loader.py

```
tests/unit/portail/test_prompt_loader.py
```

- `test_load_prompt_not_found_returns_empty` — nom inexistant → `""`
- `test_load_prompt_from_repo_dir` — fichier dans `prompts/` → contenu correct
- `test_load_prompt_user_overrides_repo` — fichier dans `~/.relais/prompts/` a priorité
- `test_load_prompt_cache_hit` — second appel sans modif mtime → pas de re-lecture disque
- `test_load_prompt_cache_invalidated_on_mtime_change` — mtime changé → contenu rechargé
- `test_list_prompts_empty_dirs` — aucun prompt → liste vide
- `test_list_prompts_deduplicates_user_and_repo` — même nom dans les deux → listé une fois
- `test_list_prompts_sorted` — résultat trié alphabétiquement

## Notes d'intégration

- `ReplyPolicy` peut être instanciée une fois dans `Portail.__init__` et sa méthode `reload()` exposée via `relais:admin:reload` (Vigile, Phase 6.1).
- `load_prompt` est prêt à être appelé depuis `atelier/soul_assembler.py` (Phase 2.2) pour charger les prompts contextuels (`out_of_hours`, `vacation`, etc.).
- L'`aiguilleur/discord/main.py` existant devra être refactorisé pour hériter de `AiguilleurBase` (Phase 5 — canaux supplémentaires).
