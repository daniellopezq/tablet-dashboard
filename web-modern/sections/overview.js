// PC/phone "Overview": every section's card on one screen.
import { h, replace } from '../lib/dom.js';

export function overview(sections) {
  return {
    id: 'overview',
    label: 'Overview',
    icon: 'shield',
    mount(root) {
      const grid = h('div', { class: 'grid' });
      replace(root, grid);
      const stops = [];
      for (const s of sections) {
        if (!s.card) continue;
        const slot = h('div', { class: `slot slot-${s.id}` });
        grid.append(slot);
        const stop = s.card(slot);
        if (typeof stop === 'function') stops.push(stop);
      }
      return () => stops.forEach((fn) => fn());
    },
  };
}
