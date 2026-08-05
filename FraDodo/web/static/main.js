
// Login Logic rimosso

let currentChart = null;

async function loadPlayerChart(discordId, activisionId) {
    // Scroll to chart
    document.getElementById('sec-stats').scrollIntoView({ behavior: 'smooth' });
    document.getElementById('chartTitle').innerText = 'Caricamento statistiche per ' + activisionId + '...';
    
    try {
        const response = await fetch('/api/player_history/' + discordId);
        const data = await response.json();
        const matches = data.matches;
        
        if (!matches || matches.length === 0) {
            document.getElementById('chartTitle').innerText = 'Nessuna partita trovata per ' + activisionId;
            document.getElementById('progressChart').style.display = 'none';
            return;
        }
        
        document.getElementById('chartTitle').innerText = 'Ultime Partite di ' + activisionId;
        document.getElementById('progressChart').style.display = 'block';
        
        const labels = matches.map((_, i) => 'Match ' + (i + 1));
        const killsData = matches.map(m => m.kills);
        const damageData = matches.map(m => m.damage);
        
        if (currentChart) {
            currentChart.destroy();
        }
        
        const ctx = document.getElementById('progressChart').getContext('2d');
        currentChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Kills',
                        data: killsData,
                        borderColor: '#4ade80',
                        backgroundColor: 'rgba(74, 222, 128, 0.2)',
                        borderWidth: 3,
                        tension: 0.4,
                        fill: true,
                        yAxisID: 'y'
                    },
                    {
                        label: 'Danni',
                        data: damageData,
                        borderColor: '#f87171',
                        backgroundColor: 'rgba(248, 113, 113, 0.2)',
                        borderWidth: 3,
                        tension: 0.4,
                        fill: false,
                        yAxisID: 'y1'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#fff' } }
                },
                scales: {
                    x: { ticks: { color: '#94A3B8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                    y: { 
                        type: 'linear', display: true, position: 'left',
                        ticks: { color: '#4ade80' }, grid: { color: 'rgba(255,255,255,0.05)' }
                    },
                    y1: {
                        type: 'linear', display: true, position: 'right',
                        ticks: { color: '#f87171' }, grid: { drawOnChartArea: false }
                    }
                }
            }
        });
    } catch (e) {
        console.error(e);
        document.getElementById('chartTitle').innerText = 'Errore nel caricamento del grafico.';
    }
}

// Confetti triggers
const btnHof = document.getElementById('btnHof');
if (btnHof) {
    btnHof.addEventListener('click', () => {
        confetti({ particleCount: 150, spread: 100, origin: { y: 0.6 }, colors: ['#F1C40F', '#F39C12', '#FFFFFF'] });
    });
}

const navHof = document.getElementById('navHof');
if (navHof) {
    navHof.addEventListener('click', () => {
        confetti({ particleCount: 150, spread: 100, origin: { y: 0.6 }, colors: ['#F1C40F', '#F39C12', '#FFFFFF'] });
    });
}

const menuItems = document.querySelectorAll('.menu-item');
menuItems.forEach(item => {
  item.addEventListener('click', (e) => {
    const href = item.getAttribute('href');
    if (href === '#' || !href) {
        e.preventDefault();
    }
    menuItems.forEach(i => i.classList.remove('active'));
    item.classList.add('active');
    
    // Smooth scroll if it's an anchor
    if (href && href.startsWith('#') && href !== '#') {
        e.preventDefault();
        const target = document.querySelector(href);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth' });
        }
    }
  });
});
