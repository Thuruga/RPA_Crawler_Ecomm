// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import { Dialog } from './Dialog'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let mounted: { container: HTMLDivElement; root: Root } | null = null

async function renderDialog(ui: React.ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  mounted = { container, root }
  await act(async () => {
    root.render(ui)
  })
  return container
}

afterEach(async () => {
  if (!mounted) return
  const { root, container } = mounted
  mounted = null
  await act(async () => root.unmount())
  container.remove()
})

describe('Dialog aboveHeader slot (MON-04, D-13)', () => {
  it('renders the aboveHeader node before the dialog header in document order', async () => {
    const container = await renderDialog(
      <Dialog title="T" onClose={() => {}} aboveHeader={<a data-testid="ver-site">Ver no site</a>}>
        body
      </Dialog>,
    )

    const aboveHeaderNode = container.querySelector('[data-testid="ver-site"]')
    const header = container.querySelector('header.dialog__header')

    expect(aboveHeaderNode).not.toBeNull()
    expect(header).not.toBeNull()

    // aboveHeaderNode must precede header in document order:
    // compareDocumentPosition on `header` relative to `aboveHeaderNode` must report FOLLOWING.
    const position = aboveHeaderNode!.compareDocumentPosition(header!)
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders no extra node before the header when aboveHeader is omitted (back-compat)', async () => {
    const container = await renderDialog(
      <Dialog title="T" onClose={() => {}}>
        body
      </Dialog>,
    )

    expect(container.querySelector('[data-testid="ver-site"]')).toBeNull()

    const panel = container.querySelector('.dialog')
    const header = container.querySelector('header.dialog__header')
    expect(header).not.toBeNull()
    // The header must be the first element child of the panel — no extra node inserted before it.
    expect(panel?.firstElementChild).toBe(header)
  })
})
