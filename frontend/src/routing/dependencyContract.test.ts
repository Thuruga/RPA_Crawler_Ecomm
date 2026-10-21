import { describe, expect, it } from 'vitest'

import packageLockJson from '../../package-lock.json?raw'
import packageManifestJson from '../../package.json?raw'

type PackageManifest = {
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
}

type PackageLock = {
  packages?: Record<string, { version?: string } | undefined>
}

const manifest = JSON.parse(packageManifestJson) as PackageManifest
const lockfile = JSON.parse(packageLockJson) as PackageLock

describe('audited routing dependencies', () => {
  it('pins only the approved direct routing and DOM packages', () => {
    expect(manifest.dependencies?.['react-router']).toBe('7.18.1')
    expect(manifest.devDependencies?.jsdom).toBe('29.1.1')
    expect(manifest.dependencies?.['react-router-dom']).toBeUndefined()
    expect(manifest.devDependencies?.['react-router-dom']).toBeUndefined()
    expect(manifest.dependencies?.['@testing-library/react']).toBeUndefined()
    expect(manifest.devDependencies?.['@testing-library/react']).toBeUndefined()
  })

  it('keeps forbidden packages out of the lockfile', () => {
    expect(lockfile.packages?.['node_modules/react-router-dom']).toBeUndefined()
    expect(lockfile.packages?.['node_modules/@testing-library/react']).toBeUndefined()
  })

  it('ensures clean portfolio dependencies without corporate auth lock-in', () => {
    expect(manifest.dependencies?.['@clerk/react']).toBeUndefined()
    expect(manifest.dependencies?.['@clerk/clerk-react']).toBeUndefined()
  })
})
