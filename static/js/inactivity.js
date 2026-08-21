/**
 * FLOW EMPRESAS - MONITOR DE INATIVIDADE
 * Controle de sessão cliente-servidor (Regra dos 5 minutos)
 */
document.addEventListener("DOMContentLoaded", () => {
    const countdownEl = document.getElementById("inactivity-countdown");
    if (!countdownEl) return;

    // Configurações herdadas do template HTML/Flask config
    const maxTimeSeconds = (window.INACTIVITY_TIMEOUT_MINUTES || 5) * 60;
    let timeRemaining = maxTimeSeconds;
    
    // Controle para pings silenciosos ao servidor
    let userWasActive = false;
    let lastPingTimestamp = Date.now();
    const pingIntervalMs = 2 * 60 * 1000; // Ping a cada 2 minutos se ativo

    /**
     * Formata os segundos restantes para MM:SS
     * @param {number} totalSeconds 
     * @returns {string}
     */
    function formatTime(totalSeconds) {
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}`;
    }

    /**
     * Atualiza o timer em tela a cada segundo e monitora a inatividade
     */
    const timerInterval = setInterval(() => {
        timeRemaining--;
        countdownEl.textContent = formatTime(timeRemaining);

        // Se estourou o tempo, encerra e redireciona
        if (timeRemaining <= 0) {
            clearInterval(timerInterval);
            console.log("[Flow Empresas] Tempo de inatividade atingido. Redirecionando para logout...");
            window.location.href = window.LOGOUT_URL;
            return;
        }

        // PING SILENCIOSO: Se o usuário está interagindo localmente (userWasActive),
        // mas não mudou de página, enviamos um ping ao backend a cada 2 minutos
        // para atualizar a chave session['last_activity'] no Flask.
        const now = Date.now();
        if (userWasActive && (now - lastPingTimestamp > pingIntervalMs)) {
            userWasActive = false;
            lastPingTimestamp = now;
            
            console.log("[Flow Empresas] Usuário ativo localmente. Sincronizando sessão com o servidor...");
            
            // Faz um fetch para a página atual (ou qualquer rota autenticada) 
            // que ativará o check_session_inactivity() no back
            fetch(window.location.href, { method: "GET" })
                .then(response => {
                    // Se o servidor retornar status de redirecionamento ou erro de auth,
                    // força o logout imediato
                    if (response.redirected || response.status === 401) {
                        window.location.href = window.LOGOUT_URL;
                    }
                })
                .catch(err => {
                    console.warn("[Flow Empresas] Erro ao sincronizar sessão no backend:", err);
                });
        }
    }, 1000);

    /**
     * Reseta o timer local e marca atividade para renovar no servidor
     */
    function registerUserActivity() {
        // Apenas redefine se o cronômetro não estiver zerado
        if (timeRemaining > 0) {
            timeRemaining = maxTimeSeconds;
            userWasActive = true;
        }
    }

    // Monitora interações típicas do usuário (com passive: true para melhor performance)
    const interactionEvents = ["mousemove", "mousedown", "keypress", "scroll", "touchstart"];
    interactionEvents.forEach(eventName => {
        document.addEventListener(eventName, registerUserActivity, { passive: true });
    });

    // Define o valor inicial na renderização
    countdownEl.textContent = formatTime(timeRemaining);
    console.log(`[Flow Empresas] Monitor de inatividade iniciado. Timeout: ${window.INACTIVITY_TIMEOUT_MINUTES} minutos.`);
});
