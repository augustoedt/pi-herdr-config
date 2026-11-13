# Pi + Herdr — configuração pessoal

Este repositório reúne configurações pessoais e a integração local Whisper usada pelo Pi. Os projetos de skills e extensões continuam independentes; este repo registra como compô-los.

## Conteúdo

- `config/settings.template.json`: preferências e lista de pacotes Pi, sem `deviceId` nem estado dinâmico.
- `config/mcp.json.example`, `config/stt.json.example`: exemplos para MCP e voz.
- `config/pi-subagents.config.json`: configuração do `pi-subagents`.
- `extensions/agents/`: perfis locais do `pi-subagents`.
- `prompts/`: templates pessoais.
- `extensions/whisper-stt-autostart.js` + `service/`: autostart e serviço Whisper local. `pi-voice-stt` continua sendo uma dependência externa separada.
- `package.json`: permite instalar a extensão Whisper e os prompts deste repo como pacote Pi.

## Herdr

O Herdr instala e gerencia `~/.pi/agent/extensions/herdr-agent-state.ts`; esse arquivo gerado **não** é versionado aqui. A instalação observada tem `HERDR_INTEGRATION_VERSION=9`. Este repo não instala o Herdr nem altera seus arquivos gerenciados. O suporte do Herdr no Linux não foi verificado: antes de contar com essa integração, siga o instalador oficial do Herdr e confirme que ele declara suporte ao Linux. Um backup antigo menciona `npm:@narumitw/pi-herdr`, mas esse pacote não aparece nas configurações atuais do Pi; não o reinstale com base apenas nesse backup.

## Instalação no Linux

Os comandos abaixo são um roteiro para Ubuntu/Debian; **ainda não foram executados em uma máquina Linux**. Em outras distribuições, use o gerenciador de pacotes equivalente. Instale Pi e, se usar essa integração, Herdr pelos canais oficiais; autentique os providers nessa máquina — este repo não transfere credenciais. O estado de suporte Linux do Herdr está descrito na seção [Herdr](#herdr).

### Primeira instalação do Pi

```sh
sudo apt update
sudo apt install git ffmpeg python3 python3-venv rsync
mkdir -p ~/Projects/Agents ~/.pi/agent
git clone https://github.com/augustoedt/pi-herdr-config.git ~/Projects/Agents/pi-herdr-config
```

Antes do primeiro início do Pi, se ainda não houver `~/.pi/agent/settings.json`:

```sh
cp ~/Projects/Agents/pi-herdr-config/config/settings.template.json ~/.pi/agent/settings.json
cp ~/Projects/Agents/pi-herdr-config/config/pi-subagents.config.json ~/.pi/agent/pi-subagents.config.json
cp ~/Projects/Agents/pi-herdr-config/config/stt.json.example ~/.pi/agent/stt.json
```

O template inclui este repo e as dependências Pi; no primeiro início, o Pi instala os pacotes declarados. Se `settings.json` já existir, **não o substitua**: execute `pi install git:github.com/augustoedt/pi-herdr-config@main` e instale individualmente apenas os pacotes do template que ainda não constarem em `pi list`. Defina seu provider/modelo padrão no Pi. Os perfis de subagents ficam em um caminho global; vincule-os ao clone apenas se o destino ainda não existir:

```sh
mkdir -p ~/.pi/agent/extensions
if [ ! -e ~/.pi/agent/extensions/agents ]; then
  ln -s ~/Projects/Agents/pi-herdr-config/extensions/agents ~/.pi/agent/extensions/agents
fi
```

Para habilitar o MCP do Railway, instale e autentique o Railway CLI e copie `config/mcp.json.example` para `~/.pi/agent/mcp.json`. Para instalar as skills próprias, clone `git@github.com:augustoedt/agent-skills.git` em `~/Projects/agent-skills` e execute `~/Projects/agent-skills/scripts/install.sh`.

### Whisper local

Crie um ambiente Python. Se usar GPU, primeiro instale nesse ambiente o PyTorch correspondente ao hardware, conforme o seletor oficial do PyTorch; depois instale Whisper:

```sh
mkdir -p ~/.local/share
python3 -m venv ~/.local/share/pi-whisper-venv
~/.local/share/pi-whisper-venv/bin/pip install --upgrade pip
~/.local/share/pi-whisper-venv/bin/pip install openai-whisper
~/.local/share/pi-whisper-venv/bin/python -c 'import whisper; whisper.load_model("large-v3")'
```

O último comando baixa o checkpoint para o cache padrão `~/.cache/whisper/`; ele ocupa vários GB e não está no Git. Configure `PI_WHISPER_PYTHON` no ambiente que inicia o Pi/Herdr:

```sh
export PI_WHISPER_PYTHON="$HOME/.local/share/pi-whisper-venv/bin/python"
```

Para persistir em sessões de terminal Bash, adicione a exportação a `~/.profile` e abra uma nova sessão:

```sh
grep -qxF 'export PI_WHISPER_PYTHON="$HOME/.local/share/pi-whisper-venv/bin/python"' ~/.profile || printf '\n%s\n' 'export PI_WHISPER_PYTHON="$HOME/.local/share/pi-whisper-venv/bin/python"' >> ~/.profile
```

Se o Herdr iniciar o Pi, configure a mesma variável no ambiente do launcher do Herdr; este repo não controla esse ambiente. A extensão inicia o serviço quando `stt.json` aponta para `127.0.0.1:10301` e o Pi abre uma sessão TUI.

### Verificação

Com Python, dependências, checkpoint e `stt.json` configurados, abra uma sessão TUI do Pi e rode `/stt doctor`. O evento de início da sessão dispara o autostart. Consulte o health check depois de abrir o Pi:

```sh
curl -fsS http://127.0.0.1:10301/health
```

Em uma instalação existente que já carrega `whisper-stt-autostart.js` diretamente de `~/.pi/agent/extensions/`, faça backup e retire essa cópia solta antes de instalar o pacote do repo, para não carregar duas cópias da extensão.

### Branches e dependências

- O template e a configuração Pi atual apontam `pi-subagents` para `git:github.com/augustoedt/pi-subagents@feat/better-subagent`; o branch já foi publicado e acompanha `origin/feat/better-subagent`.
- `pi-deepseek-cache` está fixado no commit `b33ff7f`; para atualizar, altere explicitamente essa referência no template e valide o novo commit.
- `agent-skills` permanece no seu repositório próprio; instale-o pelo clone e script indicados na seção de instalação, não copie suas skills para este repo.
- `pi-voice-stt` é a extensão externa de voz; este repo contém apenas o servidor Whisper local e o autostart que o atende.

## Fora do Git

Não versionar `auth.json`, `models-store.json`, `trust.json`, sessões, logs, bancos SQLite, caches, backups, `.env`, o modelo Whisper nem ambientes virtuais Python. As credenciais Railway continuam na autenticação local do CLI.

`web-fetch` não faz parte deste repo: sua origem é `amosblomqvist/pi-config` e não há um fork próprio registrado. Para adicioná-lo, crie e revise um fork próprio antes de incluí-lo na configuração. `quotas.json` também fica fora: pertence ao pacote externo `@latentminds/pi-quotas`, ausente da lista atual de pacotes.
