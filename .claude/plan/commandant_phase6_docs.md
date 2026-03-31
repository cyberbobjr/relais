# Phase 6 — Mise à jour documentation

**Fichiers à modifier:**
- `docs/ARCHITECTURE.md`
- `README.md`

**Complexité:** LOW

---

## 1. `docs/ARCHITECTURE.md`

### 1.1 — Taxonomie des briques (section "Transformer")

La ligne d'exemples actuellement :
```
**Exemples:** Atelier, Crieur
```

À remplacer par :
```
**Exemples:** Atelier, Le Commandant, Crieur
```

**Justification :** Le Commandant est un Transformer — il lit depuis `relais:messages:incoming`, applique une logique (parse + exécute), et écrit dans `relais:messages:outgoing:*` et `relais:memory:request`.

---

### 1.2 — Flux de données (section "PIPELINE CORE")

Dans le bloc ASCII actuel, après la ligne :
```
├─ relais:messages:incoming:* (input)
│   ▼
│ PORTAIL (consumer)
│ ├─ Valide format (Envelope)
│ ├─ Applique reply_policy
│ └─ Publie si accepté
```

Insérer (avant le `│   ▼` suivant) :
```
│
│ LE COMMANDANT (transformer — groupe parallèle à Portail)
│ ├─ Consumer group indépendant sur relais:messages:incoming
│ ├─ Détecte commandes /clear, /dnd, /brb
│ ├─ Exécute action Redis directement (hors-LLM)
│ ├─ Publie confirmation sur relais:messages:outgoing:{channel}
│ └─ ACK TOUS les messages (commandes et non-commandes)
│
│   Interaction :
│   ├─ /clear → XADD relais:memory:request {action:"clear"}
│   ├─ /dnd   → SET relais:state:dnd 1
│   └─ /brb   → DEL relais:state:dnd
│
│   PORTAIL consulte relais:state:dnd :
│   ├─ SET  → DROP silencieux (ACK, pas de forward)
│   └─ UNSET → forward normal vers relais:security
```

---

### 1.3 — Inventaire des streams : Streams intermédiaires

Ajouter une ligne dans le tableau "Streams intermédiaires" :

| Stream | Consumer | Producteur | Contenu |
|--------|----------|-----------|---------|
| `relais:state:dnd` | Portail (lecture) | Le Commandant (SET/DEL) | Clé booléenne de mode DND global (pas de TTL) |

**Position :** après la ligne `relais:memory:response`.

---

### 1.4 — Ordre d'initialisation (section "Supervisord priorities")

Remplacer dans le bloc :
```
Priority 10 (core pipeline)
  ├─ portail
  ├─ sentinelle
  ├─ atelier
  ├─ souvenir
  └─ (future: crieur, veilleur)
```

Par :
```
Priority 10 (core pipeline)
  ├─ portail
  ├─ sentinelle
  ├─ atelier
  ├─ souvenir
  ├─ commandant
  └─ (future: crieur, veilleur)
```

---

### 1.5 — Mettre à jour la date en tête de fichier

```
**Dernière mise à jour:** 2026-03-31
```
→ Mettre la date du jour au moment de l'implémentation.

---

## 2. `README.md`

### 2.1 — Diagramme ASCII (section "Diagramme ASCII")

Le bloc ASCII actuel montre le flux `Aiguilleur → PORTAIL → SENTINELLE → ATELIER`. Il faut ajouter Le Commandant comme branche parallèle à PORTAIL sur le stream `relais:messages:incoming`.

Remplacer le bloc autour de PORTAIL :

```
         └────────────────┴────────────────┘
                          │ relais:messages:incoming
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ PORTAIL — Validation des messages entrants                  │
│  Consomme : relais:messages:incoming                        │
│  Valide le format, applique reply_policy (DND, hors-heures) │
│  Produit  : relais:security                                 │
└─────────────────────────────────────────────────────────────┘
```

Par :

```
         └────────────────┴────────────────┘
                          │ relais:messages:incoming
                ┌─────────┴─────────┐
                ▼                   ▼
┌───────────────────────┐ ┌────────────────────────────────┐
│ COMMANDANT            │ │ PORTAIL — Validation           │
│  Groupe parallèle     │ │  Valide le format Envelope     │
│  Commandes hors-LLM   │ │  Vérifie relais:state:dnd      │
│  /clear /dnd /brb     │ │  Produit : relais:security     │
└──────────┬────────────┘ └──────────────┬─────────────────┘
           │ SET/DEL relais:state:dnd     │
           │ XADD memory:request(clear)  │ relais:security
           ▼                             ▼
    [réponse immédiate]         (suite du pipeline LLM)
```

---

### 2.2 — Diagramme Mermaid (section "Diagramme Mermaid")

Après la ligne :
```
AIG_IN -->|"relais:messages:incoming"| PORTAIL
```

Ajouter :
```
AIG_IN -->|"relais:messages:incoming"| COMMANDANT

subgraph COMMANDANT["COMMANDANT — Commandes hors-LLM"]
    C["Groupe parallèle à Portail\n/clear → efface contexte session\n/dnd → bloque pipeline (relais:state:dnd)\n/brb → réactive pipeline"]
end

COMMANDANT -->|"SET/DEL relais:state:dnd"| PORTAIL
COMMANDANT -->|"relais:memory:request (action=clear)"| SOUVENIR
COMMANDANT -->|"relais:messages:outgoing:{channel}"| AIG_OUT
```

Et modifier le subgraph PORTAIL pour mentionner le check DND :
```
subgraph PORTAIL["PORTAIL — Validation"]
    P["Valide le format\nConsulte relais:state:dnd\n(drop si DND actif)\nApplique reply_policy"]
end
```

---

## Ordre d'exécution de la phase

1. Implémenter les phases 1-5 en premier (le code doit exister avant de documenter)
2. Mettre à jour `docs/ARCHITECTURE.md`
3. Mettre à jour `README.md`
4. Vérifier que les diagrammes sont cohérents entre les deux fichiers

---

## Checklist de vérification documentation

- [ ] `docs/ARCHITECTURE.md` : Taxonomie mise à jour (Commandant dans les Transformers)
- [ ] `docs/ARCHITECTURE.md` : Flux de données reflète le groupe parallèle Commandant/Portail
- [ ] `docs/ARCHITECTURE.md` : `relais:state:dnd` dans l'inventaire des streams
- [ ] `docs/ARCHITECTURE.md` : Ordre d'initialisation priority 10 inclut `commandant`
- [ ] `docs/ARCHITECTURE.md` : Date "Dernière mise à jour" à jour
- [ ] `README.md` : Diagramme ASCII montre Commandant en parallèle de Portail
- [ ] `README.md` : Diagramme Mermaid inclut le subgraph COMMANDANT
