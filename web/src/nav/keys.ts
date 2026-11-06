// Use the letter produced by the keyboard, so AZERTY labels match actual input.
export const NAV_KEYS = [
  { key: 'a', action: 'tu' }, { key: 'z', action: 'f' }, { key: 'e', action: 'td' },
  { key: 'q', action: 'tl' }, { key: 's', action: 'b' }, { key: 'd', action: 'tr' },
] as const;
export type NavAction = typeof NAV_KEYS[number]['action'];
const ACTIONS: Record<string, NavAction> = Object.fromEntries(NAV_KEYS.map(({ key, action }) => [key, action]));
const ARROWS: Record<string, string> = { ArrowUp: 'z', ArrowDown: 's', ArrowLeft: 'q', ArrowRight: 'd' };

export function navigationKey(key: string): string | null {
  const letter = ARROWS[key] ?? key.toLowerCase();
  return Object.hasOwn(ACTIONS, letter) ? letter : null;
}

export function navigationAction(key: string): NavAction | null {
  const letter = navigationKey(key);
  return letter ? ACTIONS[letter] : null;
}
