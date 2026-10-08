export type Item = {
  id: string
  kind: 'suggestion' | 'memory'
  title: string
  detail: string
  sources: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'retroagent-decide': {
      items: Item[]
      hiddenUntil: string
      message: string
    }
  }
}
