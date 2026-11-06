<img width="1919" height="1030" alt="imagem" src="https://github.com/user-attachments/assets/41bbbde7-8b75-4d70-845d-b8b6408a3ab6" />
# UltraStar Video Sync 🎤

**Sincronização entre música e vídeo** — ferramenta para analisar a diferença de início entre um ficheiro MP3 e o áudio de um MP4 e sugerir o valor `#VIDEOGAP` para ficheiros de músicas UltraStar.

Programa criado por **airBN**, da comunidade [UltraStar España — Português](https://ultrastar-es.org/pt).

- ☕ [Buy Me a Coffee](https://buymeacoffee.com/airbn)
- 📷 [Instagram @airbn_ultrastares_pt](https://www.instagram.com/airbn_ultrastares_pt/)

## Funcionalidades / Features

- Pesquisa recursiva das músicas a partir da pasta selecionada.
- Comparação de áudio de MP3 e MP4 com FFmpeg, NumPy e SciPy.
- Sugestão de `#VIDEOGAP` com indicador heurístico de confiança.
- Verificação de etiquetas `#VIDEOGAP` já existentes.
- Botão **Aplicar** por música e opção de aplicação em lote.
- Preservação dos restantes bytes, da codificação e dos finais de linha do ficheiro `.txt`, com validação da escrita.
- Backups junto à música ou na pasta central `C:\UltraStar_VideoSync Backup`.
- Quatro idiomas: English, Português (Portugal), Español e Français.
- Vídeos sem áudio são ignorados.

> **Importante:** a comparação de áudio é uma estimativa e pode falhar em vídeos editados, versões diferentes da música, remisturas ou gravações ao vivo. Confirma os resultados antes de alterar toda a biblioteca.

## Executar no Windows com Python

**Requisitos:** Windows, Python 3.10 ou superior e FFmpeg (recomenda-se também FFprobe).

1. Descarrega o código do projeto (**Code → Download ZIP**) e extrai o ZIP.
2. Executa `instalar.bat` para instalar as bibliotecas.
3. Executa `iniciar.bat`.
4. Seleciona a pasta das músicas, analisa os ficheiros e aplica as sugestões que confirmares.

Se já tiveres Python e as dependências instaladas, basta executar `iniciar.bat`.

## Criar um EXE no teu computador

Num computador **Windows com Python instalado**, executa **`GERAR_EXE.bat`**. O programa compilado ficará em `dist\UltraStar_VideoSync.exe`.

## Criar um EXE no GitHub Actions

O projeto já contém `.github/workflows/build-windows.yml`.

1. Vai ao separador **Actions** do repositório.
2. Escolhe **Build Windows EXE**.
3. Clica **Run workflow** e confirma.
4. Quando a execução terminar com sucesso, abre-a e descarrega o artefacto **UltraStar_VideoSync-Windows-x64**.
5. Extrai o ZIP: no interior estará `UltraStar_VideoSync.exe`.

> O `.exe` inclui o Python e as bibliotecas da aplicação, mas **não inclui o FFmpeg**. É necessário ter FFmpeg no `PATH` do Windows ou colocar `ffmpeg.exe` junto ao `.exe` (e preferencialmente `ffprobe.exe`).

## Regra de sinal do VIDEOGAP

- O vídeo tem uma introdução extra de 12,47 segundos: `#VIDEOGAP:12,47` (avança o vídeo).
- O vídeo tem de começar 9,13 segundos mais tarde: `#VIDEOGAP:-9,13` (atrasa o vídeo).

Todas as etiquetas são escritas com **vírgula e duas casas decimais**, imediatamente antes de `#BPM` ou `#GAP`.

## Segurança

O programa guarda um backup antes de modificar o ficheiro e confirma que o conteúdo de todas as linhas que não sejam `#VIDEOGAP` permanece inalterado. Se a verificação final falhar, tenta recuperar o TXT anterior.

Ver também `LEIA-ME.txt` para informações completas.

## Direitos de utilização

O código é disponibilizado neste repositório pelo seu autor. Caso pretendas permitir reutilização, redistribuição e modificações, adiciona uma licença ao repositório (por exemplo, MIT), de acordo com as permissões que desejas conceder.
