// Kept to ES2017 syntax so browsers with modules but without ES2020 still run it.
// If the app can't load (syntax too new for this browser), fall back to the classic view.
import('./app.js').then(function (m) {
  try {
    m.start();
  } catch (err) {
    console.error('modern UI failed to start', err);
    var msg = document.getElementById('boot-msg');
    if (msg) { msg.textContent = 'Something went wrong starting the dashboard. Reload, or use the classic view.'; }
  }
}, function (err) {
  console.error('modern UI could not load', err);
  location.replace('/?ui=classic' + location.hash);
});
