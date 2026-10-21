// Runs only in browsers without ES modules: send them to the classic (ES5) dashboard.
(function () { location.replace('/?ui=classic' + location.hash); })();
