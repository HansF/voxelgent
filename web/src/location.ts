// Keep each city's assets and geographical settings together.
export const locationConfig = new URLSearchParams(location.search).get('city') === 'warsaw'
  ? { id: 'warsaw', name: 'Warsaw', data: 'data', lat: 52.23, lon: 21.01, measuredEffects: true }
  : { id: 'ghent', name: 'Ghent', data: 'ghent', lat: 51.0536, lon: 3.7249, measuredEffects: false };

export const dataUrl = (path: string) => path.startsWith('data/') ? `${locationConfig.data}/${path.slice(5)}` : path;
