import { Link } from 'react-router'

import { Button } from '../components/ui/Button'
import { ROUTES } from './paths'

export function NotFoundRoute() {
  return (
    <section className="not-found-route" aria-labelledby="not-found-title">
      <h2 id="not-found-title">Página não encontrada</h2>
      <p>Não encontramos esta página. Use uma das opções abaixo para continuar.</p>
      <div className="not-found-route__actions">
        <Link className="button button--primary button--md not-found-route__primary" to={ROUTES.monitor}>
          <span>Ir para Monitores</span>
        </Link>
        <Button variant="outline" onClick={() => window.history.back()}>Voltar</Button>
      </div>
    </section>
  )
}