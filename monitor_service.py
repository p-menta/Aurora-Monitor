import asyncio
import os
import logging
from collections import defaultdict
from typing import Dict
from dotenv import load_dotenv

from ping_service import PingService
from discord_service import DiscordService

load_dotenv()


class MonitorService:
    def __init__(self):
        # Load configuration
        self.discord_token = os.getenv('DISCORD_BOT_TOKEN')
        self.discord_channel_id = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
        self.ping_targets = os.getenv('PING_TARGETS', '').split(';')
        self.ping_interval = int(os.getenv('PING_INTERVAL', '5'))
        self.retry_attempts = int(os.getenv('RETRY_ATTEMPTS', '3'))
        self.anomaly_threshold = float(os.getenv('ANOMALY_THRESHOLD', '30'))
        self.anomaly_count = int(os.getenv('ANOMALY_COUNT', '5'))
        self.failure_percentage = float(os.getenv('FAILURE_PERCENTAGE', '50'))
        self.down_alert_failed_pings = 10
        
        # Remove empty strings from targets
        self.ping_targets = [t.strip() for t in self.ping_targets if t.strip()]
        
        # Initialize services
        self.ping_service = PingService(
            anomaly_threshold=self.anomaly_threshold,
            anomaly_count=self.anomaly_count,
            ping_interval=self.ping_interval
        )
        self.discord_service = DiscordService(
            token=self.discord_token,
            channel_id=self.discord_channel_id
        )
        
        # Set reference to this monitor service in discord service
        self.discord_service.monitor_service = self
        
        # Track failed targets
        self.failed_targets: Dict[str, bool] = defaultdict(bool)
        self.failed_ping_streak: Dict[str, int] = defaultdict(int)
        
        # Track latest latency for each target
        self.latest_latency: Dict[str, float] = {}
        
        # Logger
        self.logger = logging.getLogger("AuroraMonitor.Monitor")
        
        # Shutdown flag
        self.shutdown_requested = False
        
        self.logger.info(f"Monitoring {len(self.ping_targets)} targets")
        self.logger.info(f"Ping interval: {self.ping_interval}s, Retries: {self.retry_attempts}")
    
    async def monitor_target(self, target: str, target_type: str = "ICMP"):
        """Monitor a single target continuously"""
        
        while not self.shutdown_requested:
            try:
                success, latency, failures = await self.ping_service.ping_with_retry(
                    target, 
                    retry_attempts=self.retry_attempts
                )
                
                if success:
                    self.failed_ping_streak[target] = 0

                    # Check anomaly before inserting current sample into history
                    is_anomaly_started, is_normalized, baseline_latency = self.ping_service.check_anomaly(target, latency)

                    # Add to history for future checks
                    self.ping_service.add_to_history(target, latency)

                    # Store latest latency
                    self.latest_latency[target] = latency

                    if is_anomaly_started:
                        await self.discord_service.send_anomaly_alert(
                            target=target,
                            target_type=target_type,
                            current_latency=latency,
                            baseline_latency=baseline_latency,
                            consecutive_count=self.anomaly_count
                        )

                    if is_normalized:
                        await self.discord_service.send_latency_normalized_alert(
                            target=target,
                            target_type=target_type,
                            current_latency=latency,
                            baseline_latency=baseline_latency
                        )
                    
                    # If target was previously failed, send recovery alert
                    if self.failed_targets[target]:
                        await self.discord_service.send_target_recovered_alert(
                            target=target,
                            target_type=target_type,
                            latency=latency
                        )
                        self.failed_targets[target] = False
                    
                    # self.logger.info(f"[{target_type}] {target}: {latency:.2f}ms (OK)")
                else:
                    # Target failed all retries
                    self.failed_ping_streak[target] += 1
                    current_streak = self.failed_ping_streak[target]

                    if not self.failed_targets[target] and current_streak >= self.down_alert_failed_pings:
                        await self.discord_service.send_target_down_alert(
                            target=target,
                            target_type=target_type,
                            failed_attempts=current_streak
                        )
                        self.failed_targets[target] = True

                    self.ping_service.reset_anomaly_counter(target)
                    
                    self.logger.warning(
                        f"[{target_type}] {target}: FAILED ({failures}/{self.retry_attempts} attempts), "
                        f"streak={current_streak}/{self.down_alert_failed_pings}"
                    )
                
            except Exception as e:
                self.logger.error(f"Error monitoring {target}: {e}")
            
            await asyncio.sleep(self.ping_interval)
    
    async def check_overall_health(self):
        """Periodically check overall health and send red alert if threshold exceeded"""
        alert_sent = False
        
        while not self.shutdown_requested:
            await asyncio.sleep(30)  # Check every 30 seconds
            
            total_targets = len(self.ping_targets)
            if total_targets == 0:
                continue
            
            failed_count = sum(1 for failed in self.failed_targets.values() if failed)
            failure_rate = (failed_count / total_targets) * 100
            
            if failure_rate >= self.failure_percentage:
                if not alert_sent:
                    await self.discord_service.send_critical_alert(
                        failed_count=failed_count,
                        total_count=total_targets,
                        failure_rate=failure_rate
                    )
                    alert_sent = True
            else:
                # Reset alert flag when situation improves
                alert_sent = False
    
    def get_latency_statistics(self) -> Dict:
        """
        Get current latency statistics for all targets
        Returns dictionary with statistics for Discord command
        """
        stats = {
            'targets': [],
            'online_count': 0,
            'total_count': 0
        }
        
        # Collect targets stats
        for target in self.ping_targets:
            target_info = {
                'target': target,
                'status': 'offline' if self.failed_targets.get(target, False) else 'online',
                'current_ms': self.latest_latency.get(target, 0.0),
                'avg_ms': self.ping_service.get_average_latency(target),
                'min_ms': self.ping_service.get_min_latency(target),
                'max_ms': self.ping_service.get_max_latency(target)
            }
            stats['targets'].append(target_info)
            if target_info['status'] == 'online':
                stats['online_count'] += 1
        
        stats['total_count'] = len(self.ping_targets)
        
        return stats
    
    def request_shutdown(self):
        """Request graceful shutdown"""
        self.logger.info("Shutdown requested...")
        self.shutdown_requested = True
    
    async def start(self):
        """Start monitoring all targets"""
        self.logger.info("=" * 60)
        self.logger.info("Aurora Monitor Starting...")
        self.logger.info("=" * 60)
        self.logger.info(f"Discord Bot Token: {'Configured' if self.discord_token else 'NOT CONFIGURED'}")
        self.logger.info(f"Discord Channel ID: {self.discord_channel_id}")
        
        # Start Discord bot in background
        bot_task = asyncio.create_task(self.discord_service.start_bot())
        
        # Wait for bot to be ready
        self.logger.info("Waiting for Discord bot to connect...")
        await self.discord_service.wait_until_ready()
        self.logger.info("Discord bot is ready!")
        
        tasks = [bot_task]
        
        # Monitor all targets using ICMP
        for target in self.ping_targets:
            tasks.append(asyncio.create_task(self.monitor_target(target, target_type="ICMP")))
        
        # Overall health checker
        tasks.append(asyncio.create_task(self.check_overall_health()))
        
        # Send startup notification
        await self.discord_service.send_startup_alert(
            target_count=len(self.ping_targets),
            interval=self.ping_interval
        )
        
        self.logger.info("Monitoring started! Press Ctrl+C to stop.")
        self.logger.info("=" * 60)
        
        try:
            # Wait for all tasks
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            self.logger.info("Tasks cancelled, shutting down...")
        finally:
            # Cleanup
            await self.shutdown()
    
    async def shutdown(self):
        """Graceful shutdown"""
        self.logger.info("=" * 60)
        self.logger.info("Shutting down Aurora Monitor...")
        self.logger.info("=" * 60)
        
        # Close Discord connection
        await self.discord_service.close()
        
        self.logger.info("Shutdown complete. Goodbye!")
        self.logger.info("=" * 60)
