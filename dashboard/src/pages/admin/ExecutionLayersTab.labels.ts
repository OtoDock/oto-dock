// Provider labels shared by the Execution Layers tab's split files (.forms
// AddModelForm, .rows ModelsByProvider, .local): a leaf, so neither imports
// the other.

export const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  groq: 'Groq',
  ollama: 'Ollama',
  openai_compatible: 'OpenAI-compatible server',
}
