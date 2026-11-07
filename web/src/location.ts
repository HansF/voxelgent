// Keep each city's assets and geographical settings together.
const params = new URLSearchParams(location.search);
// ?area=centre loads the 2 km historic-centre build (public/ghent-centre) instead of the default map
const ghentData = params.get('area') === 'centre' ? 'ghent-centre' : 'ghent';
export const locationConfig = params.get('city') === 'warsaw'
  ? { id: 'warsaw', name: 'Warsaw', data: 'data', lat: 52.23, lon: 21.01, measuredEffects: true }
  : { id: 'ghent', name: 'Ghent', data: ghentData, lat: 51.0536, lon: 3.7249, measuredEffects: false };

export const dataUrl = (path: string) => path.startsWith('data/') ? `${locationConfig.data}/${path.slice(5)}` : path;
