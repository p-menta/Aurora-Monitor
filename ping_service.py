import asyncio
import statistics
import logging
from typing import Tuple, Dict
from collections import deque, defaultdict
from icmplib import async_ping


class PingService:
    def __init__(self, anomaly_threshold: float = 30.0, anomaly_count: int = 5, ping_interval: int = 5):
        self.anomaly_threshold = anomaly_threshold
        self.anomaly_count = anomaly_count
        self.ping_interval = ping_interval
        self.logger = logging.getLogger("AuroraMonitor.Ping")
        
        # Calculate history size to keep 5 minutes of data
        # 300 seconds / ping_interval = number of pings in 5 minutes
        safe_interval = max(1, ping_interval)
        history_size = max(1, 300 // safe_interval)
        
        # Store ping history for anomaly detection
        self.ping_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=history_size))
        self.anomaly_counters: Dict[str, int] = defaultdict(int)
        self.anomaly_active: Dict[str, bool] = defaultdict(bool)
        
        self.logger.info(f"Ping history will keep last {history_size} pings (~5 minutes)")
    
    async def icmp_ping(self, host: str, timeout: int = 2) -> Tuple[bool, float]:
        """
        Perform ICMP ping to host using icmplib (pure Python implementation)
        Returns: (success, latency_ms)
        """
        try:
            # Use icmplib's async_ping which doesn't require system ping command
            result = await async_ping(host, count=1, timeout=timeout, privileged=False)
            
            if result.is_alive:
                # result.avg_rtt is in milliseconds
                return True, result.avg_rtt
            else:
                return False, 0.0
                
        except PermissionError:
            # If unprivileged mode fails, try privileged mode
            try:
                result = await async_ping(host, count=1, timeout=timeout, privileged=True)
                if result.is_alive:
                    return True, result.avg_rtt
                else:
                    return False, 0.0
            except Exception as e:
                self.logger.error(f"ICMP ping error for {host} (privileged mode): {type(e).__name__}: {e}")
                return False, 0.0
                
        except Exception as e:
            self.logger.error(f"ICMP ping error for {host}: {type(e).__name__}: {e}")
            return False, 0.0
    
    async def ping_with_retry(
        self, 
        target: str, 
        retry_attempts: int = 3
    ) -> Tuple[bool, float, int]:
        """
        Ping target with retry logic using ICMP
        Returns: (overall_success, average_latency, failed_attempts)
        """
        failed_attempts = 0
        total_latency = 0
        successful_pings = 0
        
        for attempt in range(retry_attempts):
            success, latency = await self.icmp_ping(target)
            
            if success:
                total_latency += latency
                successful_pings += 1
            else:
                failed_attempts += 1
            
            # Small delay between retries
            if attempt < retry_attempts - 1:
                await asyncio.sleep(0.5)
        
        overall_success = successful_pings > 0
        avg_latency = total_latency / successful_pings if successful_pings > 0 else 0
        
        return overall_success, avg_latency, failed_attempts
    
    def add_to_history(self, target: str, latency: float):
        """Add latency measurement to target's history"""
        self.ping_history[target].append(latency)
    
    def check_anomaly(self, target: str, current_latency: float) -> Tuple[bool, bool, float]:
        """
        Check if current latency is anomalous using robust baseline (median + MAD)
        Returns: (anomaly_started, normalized, baseline_median)
        """
        history = self.ping_history[target]
        
        # Need at least 10 samples for meaningful baseline
        if len(history) < 10:
            return False, False, 0.0
        
        baseline_median = statistics.median(history)
        absolute_deviations = [abs(sample - baseline_median) for sample in history]
        mad = statistics.median(absolute_deviations)
        scaled_mad = mad * 1.4826
        percentage_threshold = baseline_median * (1 + self.anomaly_threshold / 100)
        mad_threshold = baseline_median + (3 * scaled_mad)
        threshold = max(percentage_threshold, mad_threshold)
        
        if current_latency > threshold:
            self.anomaly_counters[target] += 1
            
            if not self.anomaly_active[target] and self.anomaly_counters[target] >= self.anomaly_count:
                self.anomaly_active[target] = True
                return True, False, baseline_median

            return False, False, baseline_median
        else:
            # Reset counter if latency is normal
            self.anomaly_counters[target] = 0
            if self.anomaly_active[target]:
                self.anomaly_active[target] = False
                return False, True, baseline_median
        
        return False, False, baseline_median
    
    def get_average_latency(self, target: str) -> float:
        """Get average latency for a target"""
        history = self.ping_history[target]
        if len(history) == 0:
            return 0.0
        return statistics.mean(history)
    
    def get_min_latency(self, target: str) -> float:
        """Get minimum latency for a target"""
        history = self.ping_history[target]
        if len(history) == 0:
            return 0.0
        return min(history)
    
    def get_max_latency(self, target: str) -> float:
        """Get maximum latency for a target"""
        history = self.ping_history[target]
        if len(history) == 0:
            return 0.0
        return max(history)
    
    def reset_anomaly_counter(self, target: str):
        """Reset anomaly state for a target"""
        self.anomaly_counters[target] = 0
        self.anomaly_active[target] = False
