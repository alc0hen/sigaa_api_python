# Documentação da API — SIGAA API

Esta API expõe as funcionalidades de login e consulta do SIGAA (`sigaa.py`,
`account.py`, `bond.py`, `course.py`, ...) como uma interface web, servida
via ASGI pelo [Hypercorn](https://hypercorn.readthedocs.io/) através do
módulo `interface.py`.

Ela **não é destinada a ser chamada por usuários finais diretamente**: o
cliente é sempre outro servidor (seu backend, um bot, outro serviço), que
se autentica com um par de chaves Ed25519 e assina cada requisição. Não
existe login/senha para acessar a API em si — apenas as credenciais do
SIGAA, enviadas dentro do corpo da requisição já autenticada.

---

## 1. Subindo o servidor

### 1.1 Dependências

```
pip install -r requirements.txt
```

Isso instala `aiohttp`, `beautifulsoup4`, `lxml` (usados pelo scraper) e
`fastapi`, `hypercorn`, `cryptography` (usados pela interface web).

### 1.2 Executando

O diretório `sigaa_api_python/` é um pacote Python (usa imports relativos
como `from .sigaa import Sigaa`), então ele deve ser executado **a partir
do diretório pai**, não de dentro dele:

```bash
cd /caminho/para/o/pai/de/sigaa_api_python

# opção 1: módulo com runner próprio (usa Hypercorn internamente)
python -m sigaa_api_python.interface

# opção 2: CLI do Hypercorn apontando para o app ASGI
hypercorn sigaa_api_python.interface:app --bind 0.0.0.0:8000
```

### 1.3 Variáveis de ambiente

| Variável                     | Padrão                    | Descrição                                                                 |
|-------------------------------|----------------------------|-----------------------------------------------------------------------------|
| `SIGAA_API_BIND`              | `127.0.0.1:8000`           | Endereço/porta em que o Hypercorn escuta (usado apenas pela opção 1 acima). |
| `SIGAA_API_CLIENTS_FILE`      | `authorized_clients.json`  | Caminho do arquivo com as chaves públicas dos servidores autorizados.       |
| `SIGAA_API_SIGNATURE_TTL`     | `60`                       | Janela (segundos) de validade do timestamp de uma requisição assinada.      |
| `SIGAA_API_SESSION_TTL`       | `900`                      | Tempo (segundos) de inatividade após o qual uma sessão SIGAA é encerrada.   |
| `SIGAA_PROXY`                 | *(nenhum)*                 | Proxy HTTP opcional usado pelo `SigaaSession` ao falar com o SIGAA.         |

Um health check simples, sem autenticação, fica disponível em `GET /healthz`.

---

## 2. Autenticação: chave pública hexadecimal + assinatura

Cada servidor autorizado a chamar esta API possui um par de chaves
**Ed25519**. A chave pública, em hexadecimal, identifica o cliente e faz
parte da própria URL da requisição:

```
POST /api/v1/{client_public_key}/sessions
```

Isso não é um "API key" estático: a chave privada nunca trafega pela rede.
Em vez disso, **cada requisição é assinada** com a chave privada, e o
servidor verifica a assinatura com a chave pública correspondente. Um
request capturado não pode ser reproduzido (replay) fora da janela de
tempo configurada.

### 2.1 Cadastrando um cliente (gerando o par de chaves)

Use o CLI `manage_clients.py`, também executado a partir do diretório pai:

```bash
python -m sigaa_api_python.manage_clients generate --name "meu-servidor-backend"
```

Saída:

```
Registered client 'meu-servidor-backend' in authorized_clients.json
  public key  (goes in the request URL):      ddeeaea8cc4ef99f784c5d047d650370ef13ba9ce74297b59d73fa383863d52f
  private key (client keeps this, signs with it): c97aa11b4eb870f21d03e33d2f34bf593ab424ab0d655e4b6c6c4cabec1a29ed
```

- A **chave pública** é gravada em `authorized_clients.json` (ao lado de
  `interface.py`, ou onde `SIGAA_API_CLIENTS_FILE` apontar) — é ela que
  autoriza o cliente.
- A **chave privada** é exibida **uma única vez**. Copie-a e entregue ao
  servidor cliente por um canal seguro; ela nunca é armazenada por esta
  API.

Outros comandos:

```bash
python -m sigaa_api_python.manage_clients list
python -m sigaa_api_python.manage_clients revoke <public_key_hex>
```

### 2.2 Cabeçalhos exigidos em toda requisição autenticada

| Header         | Conteúdo                                                |
|----------------|-----------------------------------------------------------|
| `X-Signature`  | Assinatura Ed25519, em hexadecimal.                        |
| `X-Timestamp`  | Timestamp Unix (segundos) em que a requisição foi assinada. |

### 2.3 Mensagem assinada

A assinatura cobre a seguinte string, codificada em UTF-8:

```
{METODO_HTTP_MAIUSCULO}|{CAMINHO_DA_URL}|{TIMESTAMP}|{SHA256_HEX_DO_CORPO}
```

- `METODO_HTTP_MAIUSCULO`: `GET`, `POST` ou `DELETE`.
- `CAMINHO_DA_URL`: o path exato da requisição, incluindo a chave pública
  (ex.: `/api/v1/ddeeaea8.../sessions`). Sem query string, sem domínio.
- `TIMESTAMP`: o mesmo valor enviado no header `X-Timestamp`, como texto.
- `SHA256_HEX_DO_CORPO`: SHA-256, em hexadecimal, dos bytes exatos do
  corpo da requisição (`b""` para requisições sem corpo, como `GET` e
  `DELETE`).

O servidor rejeita a requisição com `401` se:
- a chave pública não estiver em `authorized_clients.json`;
- a chave pública ou a assinatura não forem hexadecimais válidas;
- o timestamp estiver ausente, ou fora da janela `SIGAA_API_SIGNATURE_TTL`
  (padrão: 60 segundos de tolerância para mais ou para menos);
- a assinatura não bater com a mensagem reconstruída pelo servidor.

Essa verificação acontece em um *middleware*, antes de qualquer roteamento
ou validação do corpo da requisição — uma requisição não autenticada nunca
recebe mais do que um `401` genérico.

### 2.4 Exemplo de assinatura (Python)

```python
import hashlib
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(PRIVATE_KEY_HEX))

def signed_headers(method: str, path: str, body: bytes) -> dict:
    timestamp = str(int(time.time()))
    body_hash = hashlib.sha256(body or b"").hexdigest()
    message = f"{method.upper()}|{path}|{timestamp}|{body_hash}".encode("utf-8")
    signature = private_key.sign(message)
    return {
        "X-Signature": signature.hex(),
        "X-Timestamp": timestamp,
        "Content-Type": "application/json",
    }
```

Uma implementação completa e executável está em `example_client.py`:

```bash
python -m sigaa_api_python.example_client <private_key_hex> http://127.0.0.1:8000
```

Se o seu servidor cliente não for escrito em Python, replique a mesma
lógica: gerar o par Ed25519 (ou usar um já gerado pelo `manage_clients.py`),
montar a string `METODO|CAMINHO|TIMESTAMP|SHA256(CORPO)` e assinar com a
chave privada. Praticamente toda linguagem tem uma biblioteca Ed25519
(`libsodium`, `PyNaCl`, `tweetnacl`, `Web Crypto API`, etc.).

---

## 3. Fluxo de uso

Todas as rotas abaixo são prefixadas por `/api/v1/{client_public_key}`;
o prefixo é omitido daqui em diante por brevidade.

1. `POST /sessions` — faz login no SIGAA e abre uma sessão.
2. `GET /sessions/{session_id}/bonds` — lista os vínculos
   (discente/docente) da conta logada.
3. `GET /sessions/{session_id}/bonds/{bond_id}/courses` — lista as
   disciplinas de um vínculo discente.
4. `GET /sessions/{session_id}/bonds/{bond_id}/courses/{course_id}/details`
   — notas, frequência e professor de uma disciplina.
5. `POST /sessions/{session_id}/bonds/{bond_id}/history` — histórico
   acadêmico completo (Turmas Anteriores).
6. `GET  /sessions/{session_id}/bonds/{bond_id}/enrollment`,
   `POST /sessions/{session_id}/bonds/{bond_id}/enrollment/selection`,
   `POST /sessions/{session_id}/bonds/{bond_id}/enrollment/confirm` — os
   três passos da matrícula online.
7. `DELETE /sessions/{session_id}` — encerra a sessão e libera os
   recursos no servidor.

Sessões ficam guardadas em memória no processo da API (não em banco de
dados) e são automaticamente fechadas após `SIGAA_API_SESSION_TTL`
segundos de inatividade. Uma sessão só pode ser lida/fechada pelo mesmo
`client_public_key` que a criou — outro cliente autenticado recebe `404`
ao tentar acessá-la.

Além da sessão HTTP do SIGAA, cada sessão guarda em memória:

- as **credenciais** enviadas no login, usadas apenas pelo modo paralelo
  do endpoint de histórico (que precisa abrir sessões SIGAA adicionais).
  Elas nunca são gravadas em disco nem devolvidas em nenhuma resposta;
- a **lista de disciplinas** do último `GET .../courses`, porque o
  `course_id` é o índice nessa lista e os objetos carregam o ViewState
  JSF necessário para navegar até as notas;
- o **estado da matrícula** (ViewState e página de confirmação), para que
  o cliente não precise carregar detalhes internos do JSF entre os
  passos.

---

## 3.1. Códigos de erro

Toda resposta de erro gerada por esta API tem o formato:

```json
{"detail": "mensagem legível", "code": "identificador_estável"}
```

O `code` existe para o cliente **reagir** ao erro sem depender do texto:

| `code`                     | Status | Significado / reação esperada                                        |
|----------------------------|--------|----------------------------------------------------------------------|
| `session_not_found`        | `404`  | Sessão inexistente, expirada ou de outro cliente → refazer o login.  |
| `sigaa_session_expired`    | `409`  | A sessão do lado do SIGAA caiu → refazer o login.                    |
| `questionnaire`            | `403`  | Questionário obrigatório bloqueando o acesso → avisar o usuário.     |
| `invalid_credentials`      | `401`  | Usuário/senha do SIGAA incorretos.                                   |
| `unknown_institution`      | `400`  | `institution` fora do `InstitutionType`.                             |
| `malformed_bond_id`        | `400`  | `bond_id` não está no formato `active:0`.                            |
| `bond_not_found`           | `404`  | Índice de vínculo inexistente.                                       |
| `teacher_bond`             | `400`  | Operação de discente pedida para vínculo docente.                    |
| `course_not_found`         | `404`  | `course_id` fora da lista atual de disciplinas.                      |
| `no_classes_selected`      | `400`  | Matrícula submetida sem nenhuma turma.                               |
| `enrollment_not_started`   | `409`  | Faltou o `GET .../enrollment` antes da seleção.                      |
| `enrollment_not_submitted` | `409`  | Faltou o `POST .../enrollment/selection` antes da confirmação.       |
| `sigaa_error`              | `502`  | Falha genérica ao conversar com o SIGAA.                             |

Falhas de **autenticação do cliente** (assinatura) continuam retornando um
`401` sem `code`, de propósito: uma requisição não autenticada não recebe
informação nenhuma sobre o que existe do outro lado.

---

## 4. Referência dos endpoints

### `GET /healthz`

Sem autenticação. Usado para checagem de disponibilidade (load balancer,
readiness probe, etc.).

**Resposta `200`:**
```json
{"status": "ok"}
```

---

### `POST /api/v1/{client_public_key}/sessions`

Faz login no SIGAA e cria uma sessão autenticada.

**Corpo da requisição:**
```json
{
  "url": "https://sigaa.ufal.br",
  "institution": "UFAL",
  "username": "usuario",
  "password": "senha"
}
```

- `institution`: um dos valores de `InstitutionType` (`types.py`):
  `IFSC`, `IFAL`, `UFAL`, `UFPE`, `UFPB`, `UNB`. Apenas `IFSC`, `IFAL`,
  `UFAL` e `UFPE` têm login implementado no momento (`sigaa.py`); as
  demais retornam `NotImplementedError` (surfaced como `502`).

**Resposta `200`:**
```json
{
  "session_id": "3f9c2b1a...",
  "name": "Fulano de Tal",
  "bonds": [
    {
      "bond_id": "active:0",
      "status": "active",
      "type": "student",
      "registration": "20231234567",
      "program": "BACHARELADO EM CIÊNCIA DA COMPUTAÇÃO"
    }
  ]
}
```

**Erros possíveis:**
| Status | Motivo                                                                 |
|--------|--------------------------------------------------------------------------|
| `400`  | `institution` desconhecida (`unknown_institution`).                      |
| `401`  | Falha na autenticação do cliente (assinatura) **ou** credenciais SIGAA inválidas (`invalid_credentials`). |
| `403`  | Questionário obrigatório bloqueando o acesso (`questionnaire`).          |
| `502`  | Erro de conexão com o SIGAA, instituição sem login implementado, etc. (`sigaa_error`). |

---

### `GET /api/v1/{client_public_key}/sessions/{session_id}/bonds`

Lista os vínculos da sessão já aberta (mesmo formato do campo `bonds` do
login).

**Resposta `200`:**
```json
{"bonds": [{"bond_id": "active:0", "status": "active", "type": "student", "registration": "...", "program": "..."}]}
```

**Erros:** `401` (assinatura inválida), `404` (sessão inexistente, expirada, ou pertencente a outro cliente).

---

### `GET /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/courses`

Lista as disciplinas do vínculo indicado por `bond_id` (o mesmo valor
retornado em `bond_id` pelos endpoints acima, ex.: `active:0`).

Esta chamada **sempre re-consulta o SIGAA** e substitui a lista guardada
na sessão. É ela que define os `id` usados pelo endpoint de detalhes, e
os objetos guardados carregam ViewStates JSF de vida curta — na prática,
liste as disciplinas e busque os detalhes logo em seguida.

**Resposta `200`:**
```json
{
  "courses": [
    {"id": 0, "title": "ESTRUTURA DE DADOS - 2024.1", "schedule_code": "2N1234"}
  ]
}
```

**Erros:**
| Status | Motivo                                                        |
|--------|-------------------------------------------------------------------|
| `400`  | `bond_id` malformado, ou vínculo é do tipo docente (sem disciplinas). |
| `401`  | Falha na autenticação do cliente.                                   |
| `404`  | Sessão ou vínculo (`bond_id`) não encontrados.                      |
| `502`  | Erro ao consultar o SIGAA.                                          |

---

### `GET /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/courses/{course_id}/details`

Notas, frequência e professor de uma disciplina, em uma única passada
(equivale a `Course.get_all_details()`, que encadeia as três navegações a
partir de uma só entrada na turma para não queimar ViewStates).

`course_id` é o índice `id` devolvido por `.../courses`. Se a sessão
ainda não tiver uma lista de disciplinas para este vínculo, ela é
carregada automaticamente.

**Resposta `200`:**
```json
{
  "id": 0,
  "title": "ESTRUTURA DE DADOS - 2024.1",
  "schedule_code": "2N1234",
  "grades": [
    {"name": "Unidade 1", "value": 8.5, "weight": 1}
  ],
  "frequency": {"total_classes": 60, "absences": 4, "percentage": 93.3},
  "professor": "FULANO DE TAL"
}
```

`grades` e `frequency` saem do parser tal como ele os produz — o formato
exato varia por instituição. `frequency` pode vir `null` e `professor`
pode vir `"Desconhecido"` quando a navegação falha; nesses casos a
requisição ainda é `200`, porque as notas continuam válidas.

**Erros:** `400` (`teacher_bond`, `malformed_bond_id`), `401`,
`403` (`questionnaire`), `404` (`session_not_found`, `bond_not_found`,
`course_not_found`), `409` (`sigaa_session_expired`), `502`.

---

### `POST /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/history`

Histórico acadêmico completo do vínculo, a partir de "Turmas Anteriores".

**Corpo da requisição** (todos os campos opcionais):
```json
{
  "cached_history": {"2024.1": [{"name": "CÁLCULO I", "grades": []}]},
  "parallel": true
}
```

- `cached_history`: o histórico que o cliente já tem guardado. As turmas
  já presentes nele são puladas, o que reduz drasticamente o tempo da
  chamada. Use o mesmo formato devolvido no campo `history`.
- `parallel` (padrão `true`): usa as credenciais guardadas na sessão para
  abrir sessões SIGAA adicionais e buscar várias turmas em paralelo.
  Com `false`, a busca é sequencial — bem mais lenta, porém sem abrir
  novas sessões no SIGAA.

**Resposta `200`:**
```json
{
  "history": {
    "2024.1": [
      {
        "name": "CÁLCULO I",
        "final_grade": 7.5,
        "absences": 2,
        "status": "APROVADO",
        "grades": [],
        "professor": "FULANA DE TAL"
      }
    ]
  }
}
```

O histórico é indexado por semestre. Turmas cuja busca falhou aparecem
com `final_grade: 0.0` e `professor: "Desconhecido"` em vez de derrubar a
requisição inteira.

**Erros:** `400`, `401`, `403` (`questionnaire`), `404`,
`409` (`sigaa_session_expired`), `502`.

---

### `GET /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment`

Passo 1 da matrícula online: as turmas em oferta. O ViewState e a URL de
ação resultantes ficam guardados na sessão para os passos seguintes.

**Resposta `200`:**
```json
{
  "levels": [
    {
      "level": "GRADUAÇÃO",
      "disciplines": [
        {"name": "CÁLCULO II", "classes": [{"id": "123456", "schedule": "2M12"}]}
      ]
    }
  ],
  "view_state": "j_id1:j_id2"
}
```

O `view_state` é devolvido apenas por conveniência de depuração — o
passo seguinte usa o valor guardado na sessão se você não mandar nada.

**Erros:** `400`, `401`, `403`, `404`, `409`, `502`.

---

### `POST /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment/selection`

Passo 2: submete as turmas escolhidas e devolve a página de confirmação
do SIGAA, para o cliente exibir ao usuário antes de confirmar.

**Corpo da requisição:**
```json
{"selected_class_ids": ["123456", "123457"], "view_state": null}
```

- `selected_class_ids`: os `id` das turmas vindos de `levels`.
- `view_state`: opcional. Omitido (ou `null`), usa o guardado na sessão
  pelo passo 1.

**Resposta `200`:**
```json
{"html": "<html>…página de confirmação…</html>", "view_state": "j_id3:j_id4"}
```

**Erros:** `400` (`no_classes_selected`), `401`, `404`,
`409` (`enrollment_not_started`), `502`.

---

### `POST /api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment/confirm`

Passo 3: pede a página de senha e a submete, em uma única chamada — o
ViewState intermediário não tem utilidade para o cliente, então ele não
sai daqui.

**Corpo da requisição:**
```json
{"password": "senha-do-sigaa"}
```

**Resposta `200`:**
```json
{"html": "<html>…página final do SIGAA…</html>"}
```

Um `200` significa apenas que o SIGAA respondeu: **o resultado da
matrícula está no HTML**. Senha incorreta e erro de pré-requisito voltam
como página normal, com um `input[type=password]` ou mensagens em
`.erros` — cabe ao cliente inspecionar antes de declarar sucesso.

**Erros:** `400`, `401`, `404`, `409` (`enrollment_not_submitted`), `502`.

---

### `DELETE /api/v1/{client_public_key}/sessions/{session_id}`

Encerra a sessão SIGAA e libera os recursos associados no servidor.

**Resposta `200`:**
```json
{"status": "closed"}
```

**Erros:** `401`, `404` (sessão inexistente ou de outro cliente).

---

## 5. Exemplo completo (`curl`)

```bash
PUB="ddeeaea8cc4ef99f784c5d047d650370ef13ba9ce74297b59d73fa383863d52f"
PRIV="<sua chave privada>"
BODY='{"url":"https://sigaa.ufal.br","institution":"UFAL","username":"...","password":"..."}'
REQ_PATH="/api/v1/$PUB/sessions"
TS=$(date +%s)
BODY_HASH=$(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)
MESSAGE="POST|$REQ_PATH|$TS|$BODY_HASH"
# assine $MESSAGE com sua chave privada Ed25519 para obter $SIG (hex)

curl -X POST "http://127.0.0.1:8000$REQ_PATH" \
  -H "Content-Type: application/json" \
  -H "X-Signature: $SIG" \
  -H "X-Timestamp: $TS" \
  -d "$BODY"
```

(`sha256sum`/`date` não assinam sozinhos — a assinatura Ed25519 em si
precisa de uma biblioteca de criptografia; veja `example_client.py` para
uma versão pronta em Python.)

---

## 6. Extensão

Novos endpoints seguem o mesmo padrão dos existentes em `interface.py`:
recebem `client_public_key` e `session_id` pela URL (a assinatura já foi
verificada pelo middleware antes de chegar ao handler), recuperam a
sessão com `_get_owned_session` e o vínculo com `_student_bond`, e
envolvem as chamadas ao scraper em `async with _scraper_errors():` — é
esse contexto que converte as exceções do scraper nos `code` da seção
3.1, de forma que a mesma falha se apresente igual em qualquer endpoint.


