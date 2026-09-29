# 🐸 Ecossistema SAPO - Scripts QGIS Processing

Coleção de scripts de processamento geoespacial desenvolvidos para o **QGIS (Processing Toolbox)** voltados à automação, validação topológica, estruturação de dados vetoriais e controle de qualidade cartográfica segundo especificações técnicas (como **EDGV** / **ET-ADGV**).

---

## 📑 Sumário dos Scripts SAPO

| Script | Versão | Grupo | Descrição Resumida |
| :--- | :---: | :---: | :--- |
| **[`sapo_gerar_poligonos.py`](#1--sapo---gerar-polígonos-sapo_gerar_poligonospy)** | `1.0.0` | 🐸 SAPO | Pipeline autossuficiente em 4 etapas para geração e classificação de polígonos e cobertura terrestre. |
| **[`sapo_verificar_ligacao.py`](#2--sapo---verificar-ligação-entre-bancos-sapo_verificar_ligacaopy)** | `1.0.2` | 🐸 SAPO | Verificação de continuidade, ligação e divergência de atributos entre molduras/cartas de bancos vizinhos. |
| **[`sapo_corrigir_drenagem.py`](#3--sapo---corrigir-drenagem-sapo_corrigir_drenagempy)** | `1.0.0` | 🐸 SAPO | Pipeline em 4 etapas para detecção, traçado de esqueleto central e correção de conflitos drenagem x curvas de nível. |

---

## 1. 🏗️ SAPO - Gerar Polígonos (`sapo_gerar_poligonos.py`)

Pipeline unificado e 100% autossuficiente (sem necessidade de scripts externos) para transformar delimitadores lineares e feições vetoriais em polígonos estruturados e classificados.

### 🌟 Principais Funcionalidades
- **Execução Modular (4 Passos):**
  1. **Passo 1:** Geração de delimitadores auxiliares (`aux_delimitadores_l` e `aux_delimitadores_area_edif_massa_dagua_l`).
  2. **Passo 2:** Geração de polígonos de edificação e massa d'água (`area_edificada_area`, `massa_dagua_area`), com bloqueio e identificação de conflitos impeditivos.
  3. **Passo 3:** Geração de delimitadores de cobertura terrestre (`aux_delimitadores_cobertura_l`).
  4. **Passo 4:** Geração e classificação de polígonos de cobertura terrestre com relatório final de consistência.
- **Organização Automática:** Todas as camadas geradas são agrupadas no grupo `Sapo_poligonos`.
- **Limpeza Inteligente:** Controle via checkbox para exclusão automática de delimitadores temporários após a geração dos polígonos.

---

## 2. 🔍 SAPO - Verificar Ligação Entre Bancos (`sapo_verificar_ligacao.py`)

Ferramenta avançada para validação de ligação e continuidade cartográfica entre bancos de dados ou cartas topográficas adjacentes.

### 🌟 Principais Funcionalidades
- **Seleção Inteligente de Molduras:** Identifica e lista automaticamente molduras vetoriais não vazias no projeto com atalhos de seleção rápida.
- **Detecção Mútua na Divisa:** Identifica conexões de linhas e áreas que tocam na fronteira compartilhada entre duas ou mais molduras selecionadas.
- **Comparação Rigorosa de Atributos:** Compara atributo por atributo entre o elemento de origem e o vizinho correspondente (no raio configurável de **1 metro**).
  - *Campos ignorados automaticamente:* metadados com prefixo `_`, `id`, `fid`, `gid`, `observacao`, datas, operadores, `length_otf`, `area_otf`.
- **Deduplicação Bilateral (Sem Flags Duplicados):** Sistema de pareamento mútuo que consome as duas pontas da conexão, garantindo que cada inconsistência gere **apenas 1 flag** no mapa, sem duplicatas reversas ($A \rightarrow B$ e $B \rightarrow A$).
- **Camadas de Diagnóstico:** Organizadas no grupo `Sapo - Validação de Ligação entre Bancos (1m)`:
  - 🟢 **`Ligacoes_Validadas_OK`**: Conexões com continuidade perfeita e atributos idênticos.
  - 🟡 **`Erros_Atributos_Divergentes`**: Conexões espaciais válidas, porém com discrepância nos atributos (detalhamento claro de quais campos diferem).
  - 🔴 **`Erros_Sem_Ligacao`**: Pontas soltas (feições que tocam a divisa sem elemento correspondente no banco vizinho).
- **Contagem Padrão de Feições (`[N]`):** As camadas de resultado já são criadas com `showFeatureCount` habilitado por padrão no painel de camadas do QGIS.

---

## 3. 💧 SAPO - Corrigir Drenagem (`sapo_corrigir_drenagem.py`)

Pipeline modular e autossuficiente para identificação, traçado de esqueleto central e correção geométrica de inconsistências entre hidrografia (trechos de drenagem) e relevo (curvas de nível).

### 🌟 Principais Funcionalidades
- **Execução Modular em 4 Etapas (Checkboxes Independentes):**
  1. **Etapa 1 (Detecção de Conflitos):** Identifica trechos de hidrografia com $\ge 2$ toques na mesma curva de nível (conflitos de reentrância e cortes indevidos de espigão). Gera a camada de pontos `flags_drenagem_curva` com fluxo ordenado (montante $\rightarrow$ jusante). Se a camada já existir no projeto, pula o cálculo inicial.
  2. **Etapa 2 (Traçado do Eixo Central / Esqueleto):** Traça os novos eixos centrais dos talvegues na camada temporária `esqueleto_curva_bbox` via densificação, esqueleto de Voronoi e caminho de menor custo (Dijkstra) com suavização de Chaikin. A camada é preservada no projeto e, se já existir, pula automaticamente as Etapas 1 e 2 para ir direto à atualização.
  3. **Etapa 3 (Atualização da Camada de Drenagem):** Executa **backup prévio de segurança** em GeoPackage (`.gpkg`) e emenda os eixos gerados diretamente na camada de drenagem, substituindo a geometria conflituosa e preservando os atributos originais.
  4. **Etapa 4 (Revalidação e Saldo Final de Flags):** Substitui a camada de flags anterior recalculando as interseções na drenagem atualizada, apresentando o saldo final exato de pendências para conferência/edição manual refinada.
- **Processamento de Seleção:** Opção para processar apenas feições selecionadas (nas flags ou esqueletos), permitindo ajustes pontuais e graduais.
- **Segurança Operacional:** Pré-validação com exportação obrigatória de backup em arquivo `.gpkg` antes de efetuar alterações na hidrografia.
- **Organização Automática:** As camadas resultantes são agrupadas em `Sapo_Drenagem_Curva` com ativação automática da contagem de feições (`showFeatureCount`).

---

## 🛠️ Instalação e Atualização no QGIS

### Diretório Padrão dos Scripts
Coloque os arquivos `.py` na pasta de scripts de processamento do perfil do QGIS:

- **Windows:**
  ```text
  C:\Users\<Seu-Usuario>\AppData\Roaming\QGIS\QGIS3\profiles\default\processing\scripts\
  ```
- **Linux:**
  ```text
  ~/.local/share/QGIS/QGIS3/profiles/default/processing/scripts/
  ```

### Como Atualizar a Lista de Scripts no QGIS
Para recarregar exclusivamente os scripts na **Caixa de Ferramentas do Processing** sem precisar reiniciar o QGIS, abra o **Console Python** (`Ctrl + Alt + P`) e execute:

```python
from qgis.core import QgsApplication

# Atualiza exclusivamente o provedor de scripts do Processing
provider = QgsApplication.processingRegistry().providerById('script')
if provider:
    provider.refreshAlgorithms()
    print("🐸 Scripts SAPO atualizados com sucesso!")
```

---

## 👨‍💻 Autoria e Créditos

- **Autor:** Marcos Batista (Sgt Topo / 13)
- **E-mail:** [mbsj2007@hotmail.com](mailto:mbsj2007@hotmail.com)
- **GitHub:** [m4rc05-34t15t4](https://github.com/m4rc05-34t15t4)
- **Ecossistema:** Ferramentas SAPO para produção, aquisição e controle de qualidade em geoinformação.
