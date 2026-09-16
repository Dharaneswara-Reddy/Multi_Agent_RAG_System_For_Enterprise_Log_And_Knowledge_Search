# Diagrams

PlantUML sources for the diagrams in the root README. Each one is rendered
twice — light for GitHub's light theme, dark (into `dark/`) for its dark theme —
and the README serves whichever matches the reader, using `<picture>`.

```bash
# PlantUML needs Java and Graphviz:  sudo dnf install java-21-openjdk graphviz
curl -sSLo /tmp/plantuml.jar \
  https://github.com/plantuml/plantuml/releases/download/v1.2025.4/plantuml-1.2025.4.jar

cd docs/diagrams
for f in architecture retrieval deployment cicd; do
  java -jar /tmp/plantuml.jar -tpng -Sdpi=160          "$f.puml"
  java -jar /tmp/plantuml.jar -tpng -Sdpi=160 -DDARK=1 -o dark "$f.puml"
done
```

| Source | Shows |
|---|---|
| `architecture.puml` | One question, end to end: triage → agents → retrieval → synthesis → guardrail gate |
| `retrieval.puml` | The four retrieval stages, each labelled with what it measurably bought |
| `deployment.puml` | The AWS demo deployment, as applied from `infra/demo/` |
| `cicd.puml` | CI, the evaluation quality gate, and the deploy that only runs after it passes |

`style.puml` holds the shared palette and skin. It defines both themes and
switches on the `DARK` preprocessor variable, so the two renders can never drift
apart in layout — only in colour.

**Keep the diagrams true.** They describe what is deployed, not what is planned;
if the Terraform or the graph changes, re-render before the README claims
otherwise.
