# Base image from Playwright to include all browser dependencies.
#
# A tag PRECISA casar com o pin de `playwright` em requirements.txt: a imagem ja
# traz os browsers da sua propria versao, e se o pip instalar outra versao o
# `playwright install` baixa um segundo conjunto completo (~690 MB desperdicados).
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Copy requirements first for cache optimization
COPY requirements.txt .

# Toolchain de compilacao, dependencias Python e remocao do toolchain -- tudo em
# UMA layer, senao os 248 MB do build-essential ficam gravados na imagem final
# mesmo depois do purge (layer removida nao devolve espaco).
#
# O torch vem do indice CPU-only da PyTorch DE PROPOSITO: o wheel do PyPI arrasta
# o stack CUDA inteiro (nvidia 2,7 GB + triton 690 MB + torch 1,1 GB) para um
# container headless que nao tem GPU alguma. O CLIP em
# services/image_ai_service.py roda igual em CPU. Instalado ANTES do
# requirements.txt para que o `transformers` encontre o torch ja satisfeito e nao
# o substitua pela build CUDA.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Gera o client Python do Prisma. Sem este passo qualquer `from prisma import
# Prisma` levanta RuntimeError("The Client hasn't been generated yet") e o
# backend nem chega a subir. Copiado antes do resto do codigo de proposito: o
# client e gravado dentro do pacote instalado, entao esta layer nao precisa ser
# refeita a cada alteracao de fonte.
#
# `prisma-python/nodeenv` (453 MB) e um Node completo usado SO para rodar o CLI
# aqui no build -- fora da imagem final. Removido nesta MESMA layer, senao nao
# devolve espaco.
#
# NAO apagar `prisma-python/` inteiro: `prisma-python/binaries` guarda o
# query-engine que o prisma-client-py carrega em RUNTIME, e sem ele o boot morre
# com `BinaryNotFoundError` na conexao. (`prisma/master` e o cache do CLI Node,
# nao serve para o cliente Python.)
# A URL abaixo serve apenas para validar o datasource durante a geracao. O
# comando nao conecta ao banco, a variavel fica restrita a esta layer e a URL
# real continua sendo fornecida ao container somente em runtime.
COPY prisma ./prisma
RUN DATABASE_URL="postgresql://prisma:prisma@localhost:5432/prisma?schema=scraping" prisma generate \
    && rm -rf /root/.cache/prisma-python/nodeenv

# Copy the rest of the application
COPY . .

# Expose the API port
EXPOSE 8080

# Command to run the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
