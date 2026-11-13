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

O Herdr instala e gerencia `~/.pi/agent/extensions/herdr-agent-state.ts`; esse arquivo gerado **não** é versionado aqui. A instalação atual tem um arquivo marcado `HERDR_INTEGRATION_VERSION=9`. Um backup antigo das configurações menciona `npm:@narumitw/pi-herdr`, mas esse pacote não aparece nas configurações atuais do Pi. Não reinstale nem fixe essa dependência só com base no backup: confirme o mecanismo atualmente usado pelo Herdr e mantenha a integração sob gestão dele.

## Restaurar no Pi

1. Instale Pi e Herdr e autentique os providers separadamente.
2. Publique este repo no GitHub privado. Depois, instale como pacote Pi:

   ```sh
   pi install git:github.com/augustoedt/pi-herdr-config@main
   ```

   Antes da publicação, para teste local:

   ```sh
   pi install "$HOME/Projects/Agents/pi-herdr-config"
   ```

3. Compare `config/settings.template.json` com as configurações locais; não substitua `settings.json` inteiro sem revisar.
4. Copie os exemplos necessários para `~/.pi/agent/` e coloque os perfis de `extensions/agents/` em `~/.pi/agent/extensions/agents/` (idealmente com links pelo script de bootstrap futuro).
5. Instale os forks e skills listados no template; autenticações e credenciais não vêm deste repo.
6. Prepare o Python com `openai-whisper`/`torch`, FFmpeg e o checkpoint `~/.cache/whisper/large-v3.pt` para usar Whisper.

### Branches e dependências

- O template e a configuração Pi atual apontam `pi-subagents` para `git:github.com/augustoedt/pi-subagents@feat/better-subagent`; o branch já foi publicado e acompanha `origin/feat/better-subagent`.
- `pi-deepseek-cache` está fixado no commit atualmente usado; a referência pode ser alterada depois, se preferir seguir `main`.
- `agent-skills` permanece no seu repositório próprio e deve ser instalado por ele, não copiado para este repo.
- `pi-voice-stt` é a extensão externa de voz; este repo contém apenas o servidor Whisper local e o autostart que o atende.

## Fora do Git

Não versionar `auth.json`, `models-store.json`, `trust.json`, sessões, logs, bancos SQLite, caches, backups, `.env`, o modelo Whisper nem ambientes virtuais Python. As credenciais Railway continuam na autenticação local do CLI.

`web-fetch` foi omitido por enquanto, conforme a decisão de avaliar sua remoção. `quotas.json` também foi omitido: pertence ao pacote externo `@latentminds/pi-quotas`, que não está na lista atual de pacotes.
