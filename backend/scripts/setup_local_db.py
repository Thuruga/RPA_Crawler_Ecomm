import os
import sys
import psycopg2
from dotenv import load_dotenv

# Carrega o .env local
load_dotenv()

def main():
    if os.environ.get("ENVIRONMENT") != "development":
        print("ERRO: Este script deve ser rodado apenas no ENVIRONMENT=development.")
        sys.exit(1)
        
    if "--force-drop-danger" not in sys.argv:
        print("ERRO: Flag --force-drop-danger é obrigatoria para executar o script destrutivo.")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if db_url and "?" in db_url:
        db_url = db_url.split("?")[0]
    if not db_url or "localhost" not in db_url:
        print("ERRO: DATABASE_URL nao configurada corretamente para localhost.")
        sys.exit(1)

    # Conecta ao postgres default para criar database se precisar
    try:
        print("Conectando ao banco para recriar schema...")
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS scraping CASCADE;")
            cur.execute("CREATE SCHEMA scraping;")
            cur.execute("ALTER USER postgres CREATEDB;")
            print("Schema recriado com sucesso.")
            
        conn.close()

        # Rodar o script DDL
        print("Aplicando 001_initial_schema.sql...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        migration_path = os.path.join(script_dir, '..', 'migrations', '001_initial_schema.sql')
        
        with open(migration_path, 'r', encoding='utf-8') as f:
            sql = f.read()

        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
            
        conn.close()
        print("Configuração local concluida!")

    except Exception as e:
        print(f"Erro ao configurar DB local: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
