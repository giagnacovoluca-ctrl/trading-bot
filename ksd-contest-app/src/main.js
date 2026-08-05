import './style.css'

document.querySelector('#app').innerHTML = `
  <div id="loginOverlay" class="login-overlay">
    <div class="login-card">
      <i class="fa-brands fa-discord" style="font-size: 48px; color: #5865F2; margin-bottom: 20px;"></i>
      <h2>Accedi al KSD Contest</h2>
      <p>Collega il tuo account Discord per vedere le tue statistiche personali e la classifica.</p>
      <button class="btn-discord" id="loginBtn">
        <i class="fa-brands fa-discord"></i> Login con Discord
      </button>
    </div>
  </div>

  <nav class="sidebar">
    <div class="sidebar-header">
      <i class="fa-solid fa-trophy brand-icon"></i>
      <span class="brand-text">KSD CONTEST</span>
    </div>
    <div class="sidebar-menu">
      <a href="#" class="menu-item active">
        <i class="fa-solid fa-gamepad menu-icon"></i>
        <span>Contest</span>
      </a>
      <a href="#" class="menu-item" id="navClassifica">
        <i class="fa-solid fa-chart-simple menu-icon" style="color: var(--accent-green)"></i>
        <span>Classifica</span>
      </a>
      <a href="#" class="menu-item">
        <i class="fa-solid fa-square-check menu-icon" style="color: var(--accent-green)"></i>
        <span>Approvazioni</span>
      </a>
      <a href="#" class="menu-item" id="navHof">
        <i class="fa-solid fa-medal menu-icon" style="color: var(--accent-gold)"></i>
        <span>Hall of Fame</span>
      </a>
      <a href="#" class="menu-item">
        <i class="fa-solid fa-scroll menu-icon" style="color: #F5DEB3"></i>
        <span>Log Contest</span>
      </a>
      <a href="#" class="menu-item">
        <i class="fa-solid fa-gear menu-icon" style="color: var(--text-muted)"></i>
        <span>Staff Panel</span>
      </a>
    </div>
  </nav>

  <main class="main-content">
    <header class="top-bar">
      <div class="page-title">
        <i class="fa-solid fa-trophy"></i>
        <span>Contest Dashboard</span>
      </div>
      <div class="user-profile">
        <span id="userNameDisplay" style="font-weight: 600; font-size: 14px;">Ospite</span>
        <div class="avatar" id="userAvatar">
          ?
          <div class="status-indicator" style="background-color: #666"></div>
        </div>
      </div>
    </header>

    <div class="content-wrapper">
      <div class="contest-card">
        <div class="card-header">
          <h1 class="card-title">KASH!DO Contest Manager v3</h1>
          <p class="card-subtitle">Usa i pulsanti qui sotto per gestire il contest.</p>
        </div>

        <div class="features-list">
          <div class="feature-item">
            <div class="feature-icon-wrapper green">
              <i class="fa-solid fa-circle-play"></i>
            </div>
            <div class="feature-content">
              <h3>Invia Risultato</h3>
              <p>Invia la tua partita per l'approvazione dello Staff.</p>
            </div>
          </div>
          
          <div class="feature-item">
            <div class="feature-icon-wrapper gold">
              <i class="fa-solid fa-trophy"></i>
            </div>
            <div class="feature-content">
              <h3>Classifica</h3>
              <p>Visualizza la classifica aggiornata del contest.</p>
            </div>
          </div>

          <div class="feature-item">
            <div class="feature-icon-wrapper blue">
              <i class="fa-solid fa-user"></i>
            </div>
            <div class="feature-content">
              <h3>Profilo</h3>
              <p>Controlla le tue statistiche personali.</p>
            </div>
          </div>
        </div>

        <div class="action-buttons">
          <button class="btn btn-primary" id="btnInvia">
            <i class="fa-solid fa-circle" style="font-size: 12px; color: #4ade80;"></i> Invia Risultato
          </button>
          <button class="btn btn-secondary" id="btnClassifica">
            <i class="fa-solid fa-trophy" style="color: #F1C40F;"></i> Classifica
          </button>
          <button class="btn btn-dark" id="btnProfilo">
            <i class="fa-solid fa-user"></i> Profilo
          </button>
          <button class="btn btn-dark">
            <i class="fa-solid fa-chart-column" style="color: #a78bfa;"></i> Statistiche
          </button>
          <button class="btn btn-dark" id="btnHof">
            <i class="fa-solid fa-medal" style="color: #F1C40F;"></i> Hall Of Fame
          </button>
          <button class="btn btn-danger">
            <i class="fa-solid fa-gear"></i> Staff
          </button>
        </div>

        <div class="chart-container">
          <canvas id="progressChart"></canvas>
        </div>

        <div class="status-message">
          <i class="fa-solid fa-check" style="color: #4ade80;"></i>
          <span style="color: #4ade80;">Ultima partita approvata! +15.5 pt</span>
          <span class="muted">Oggi alle 15:42</span>
        </div>
      </div>
    </div>
  </main>
\`

// Login Logic
document.getElementById('loginBtn').addEventListener('click', () => {
  const overlay = document.getElementById('loginOverlay');
  
  // Fake Discord Auth Flow
  overlay.innerHTML = '<div class="login-card"><h2><i class="fa-solid fa-spinner fa-spin"></i> Autenticazione...</h2></div>';
  
  setTimeout(() => {
    overlay.style.opacity = '0';
    setTimeout(() => overlay.style.display = 'none', 500);
    
    // Update UI
    document.getElementById('userNameDisplay').innerText = 'FraDodo';
    document.getElementById('userAvatar').innerHTML = 'F<div class="status-indicator"></div>';
    
    // Trigger login Confetti!
    confetti({
      particleCount: 100,
      spread: 70,
      origin: { y: 0.6 },
      colors: ['#5865F2', '#23A559', '#F1C40F']
    });
    
    initChart();
  }, 1500);
});

// Chart Logic
function initChart() {
  const ctx = document.getElementById('progressChart').getContext('2d');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: ['Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato'],
      datasets: [{
        label: 'Kills Medie',
        data: [4, 6, 5, 8, 12, 15],
        borderColor: '#5865F2',
        backgroundColor: 'rgba(88, 101, 242, 0.2)',
        borderWidth: 3,
        tension: 0.4,
        fill: true
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#fff' } }
      },
      scales: {
        y: { ticks: { color: '#94A3B8' }, grid: { color: 'rgba(255,255,255,0.05)' } },
        x: { ticks: { color: '#94A3B8' }, grid: { color: 'rgba(255,255,255,0.05)' } }
      }
    }
  });
}

// Confetti triggers
document.getElementById('btnHof').addEventListener('click', () => {
    confetti({ particleCount: 150, spread: 100, origin: { y: 0.6 }, colors: ['#F1C40F', '#F39C12', '#FFFFFF'] });
});
document.getElementById('navHof').addEventListener('click', () => {
    confetti({ particleCount: 150, spread: 100, origin: { y: 0.6 }, colors: ['#F1C40F', '#F39C12', '#FFFFFF'] });
});

const menuItems = document.querySelectorAll('.menu-item');
menuItems.forEach(item => {
  item.addEventListener('click', (e) => {
    e.preventDefault();
    menuItems.forEach(i => i.classList.remove('active'));
    item.classList.add('active');
  });
});
