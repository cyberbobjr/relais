# RELAIS — Guide de contribution

**Dernière mise à jour :** 2026-04-17

Ce guide décrit le workflow de contribution réellement adapté au dépôt actuel.

---

## Setup développement

### Prérequis

- Python `>=3.11`
- `uv`
- Redis local si vous voulez lancer le pipeline
- `supervisord` si vous utilisez le mode supervisé

### Installation locale

```bash
git clone <repo-url>
cd relais

uv sync

cp .env.example .env

python -c "from common.init import initialize_user_dir; initialize_user_dir()"

alembic upgrade head
```

### Répertoire de travail

Par défaut, RELAIS utilise `./.relais` à la racine du dépôt. Vous pouvez le surcharger avec `RELAIS_HOME`.

`initialize_user_dir()` :

- crée la structure de travail sous `RELAIS_HOME`
- copie les templates livrés
- n'écrase jamais les fichiers existants

Il est correct que les points d'entrée de briques l'appellent au démarrage.

### Démarrage local

#### Option recommandée

```bash
./supervisor.sh start all           # Démarrer le système
./supervisor.sh --verbose start all # Démarrer + suivre les logs (Ctrl+C pour détacher)
./supervisor.sh status              # Voir l'état des bricks
./supervisor.sh --verbose restart all # Redémarrer + suivre les logs
```

#### Option manuelle

```bash
redis-server config/redis.conf

uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

---

## Architecture des tests

Le dépôt utilise principalement :

- tests unitaires avec `pytest`
- tests async avec `pytest-asyncio`
- mocks `unittest.mock`
- `fakeredis` pour certains tests d'intégration locale

Les marqueurs présents dans `pyproject.toml` sont :

- `unit`
- `integration`

### Types de tests utiles

- tests unitaires de loader/config : ex. `tests/test_channel_config.py`
- tests unitaires de briques : ex. `tests/test_commandant.py`, `tests/test_soul_assembler.py`
- smoke / intégration de pipeline : ex. `tests/test_smoke_e2e.py`

### Commandes courantes

```bash
uv run pytest tests/ -v

uv run pytest tests/test_channel_config.py -v

uv run pytest tests/test_smoke_e2e.py -v
```

Avec couverture :

```bash
uv run pytest tests/ --cov=common,portail,sentinelle,atelier,souvenir,aiguilleur,archiviste,commandant --cov-report=term-missing
```

---

## Contribuer à une brique existante

### Règles pratiques

- partir du `main.py` de la brique pour comprendre le flux réel
- vérifier les streams Redis réellement consommés et produits
- regarder les tests existants avant d’introduire de nouveaux patterns
- mettre à jour la doc si vous changez un stream, une variable d’environnement ou un point d’entrée

### Références utiles

- [README.md](/Users/benjaminmarchand/IdeaProjects/relais/README.md)
- [docs/ARCHITECTURE.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ARCHITECTURE.md)
- [docs/ENV.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ENV.md)

---

## Ajouter une nouvelle brique

### Checklist minimale

- créer un package dédié avec un `main.py`
- utiliser `RedisClient("<nom_de_brique>")`
- appeler `initialize_user_dir()` dans le bloc `__main__`
- documenter les streams consommés et produits
- ajouter les tests ciblés
- ajouter la brique dans `supervisord.conf` si elle doit être démarrée avec le reste du système
- mettre à jour le README et `docs/ARCHITECTURE.md`

### Pattern minimal

Toutes les briques héritent de `common.brick_base.BrickBase`. Le patron minimal :

```python
import asyncio

from common.brick_base import BrickBase, StreamSpec
from common.shutdown import GracefulShutdown  # noqa: F401 — point de patch pour les tests


class MyBrick(BrickBase):
    def __init__(self) -> None:
        super().__init__("mybrick")

    def _create_shutdown(self) -> GracefulShutdown:
        return GracefulShutdown()

    def _load(self) -> None:
        pass  # charger le YAML dans les attributs self ici

    def stream_specs(self) -> list[StreamSpec]:
        return [StreamSpec(stream="relais:my:stream", group="mybrick_group",
                           consumer="mybrick_1", handler=self._handle)]

    async def _handle(self, envelope, redis_conn) -> bool:
        ...
        return True  # True = XACK, False = laisser dans le PEL


if __name__ == "__main__":
    asyncio.run(MyBrick().start())
```

### Points d’attention

- `RedisClient` s’appuie sur `REDIS_PASS_<BRICK>` puis `REDIS_PASSWORD`
- le dépôt privilégie les consumer groups Redis pour les boucles de consommation
- les ACK sont gérés explicitement dans les boucles de lecture

---

## Ajouter un canal Aiguilleur

L’entrée du superviseur de canaux est [aiguilleur/main.py](/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/main.py). Les canaux sont configurés via `config/aiguilleur.yaml` puis chargés par `load_channels_config()`.

### Deux modes pris en charge

- `type: native` : adaptateur Python chargé dynamiquement
- `type: external` : sous-processus supervisé par l’Aiguilleur

### Pour un canal natif

- créer un module `aiguilleur/channels/<canal>/adapter.py`
- exposer une classe `*Aiguilleur`
- implémenter le contrat attendu par `AiguilleurManager`
- publier les messages entrants sur `relais:messages:incoming`
- consommer `relais:messages:outgoing:<canal>` pour la sortie

L’exemple de référence actuel est [aiguilleur/channels/discord/adapter.py](/Users/benjaminmarchand/IdeaProjects/relais/aiguilleur/channels/discord/adapter.py).

### Pour un canal externe

Déclarer dans `aiguilleur.yaml` :

```yaml
channels:
  mychannel:
    enabled: true
    streaming: false
    type: external
    command: node
    args:
      - adapters/mychannel/index.js
```

### Configuration

`config/aiguilleur.yaml` n’est pas copié automatiquement par `initialize_user_dir()`. Si vous ajoutez un canal, pensez à :

- mettre à jour [config/aiguilleur.yaml.default](/Users/benjaminmarchand/IdeaProjects/relais/config/aiguilleur.yaml.default)
- documenter la création manuelle du fichier override dans `RELAIS_HOME/config/aiguilleur.yaml`

---

## Conventions de code

- Python 3.11+
- type hints partout où c’est utile
- docstrings courtes mais concrètes
- tests pour tout changement de flux, de config ou de contrat Redis
- garder la doc ancrée dans le code réel, pas dans une architecture visée

---

## Avant d’ouvrir une PR

- vérifier que les points d’entrée documentés existent toujours
- vérifier les streams et variables d’environnement modifiés
- lancer les tests ciblés touchés par votre changement
- relire `README.md`, `docs/ARCHITECTURE.md` et `docs/ENV.md` si vous changez le comportement public
