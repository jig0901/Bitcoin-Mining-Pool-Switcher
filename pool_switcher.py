"""
Pool Switcher + Rebooter – Selenium automation for Antminer S19j Pro & Whatsminer M31S+
====================================================================================
Switch ASIC miners between different pool presets **and/or reboot them** from the
command line or on a cron schedule.

### New CLI flags
* `--pool <key>` – on‑demand pool switch (existing)
* `--reboot`     – reboot selected miners after switching – **or** reboot alone

Examples:
```bash
# Just reboot every miner
python pool_switcher.py --config config.yaml --reboot

# Switch to solo *and* reboot ant01 afterwards
python pool_switcher.py --config config.yaml --pool solo --miner ant01 --reboot
```
If neither `--pool` nor `--reboot` is provided the script drops into its
scheduled mode exactly as before.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

import yaml
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------------

def _wait(driver: webdriver.Chrome, timeout: int, condition):
    return WebDriverWait(driver, timeout).until(condition)


def _new_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    # opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })
    drv = webdriver.Chrome(options=opts)
    drv.set_page_load_timeout(30)
    return drv

# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class Pool:
    url: str
    worker: str
    password: str = "x"

# ------------------------------------------------------------------
# Abstract Miner class
# ------------------------------------------------------------------

class MinerBase:
    """
    A base class for managing and interacting with a mining device. This class provides
    a framework for switching mining pools and rebooting the device. Subclasses must
    implement the required methods for specific device interactions.
    Attributes:
        name (str): The name of the miner.
        ip (str): The IP address of the miner.
        username (str): The username for logging into the miner. Defaults to "root".
        password (str): The password for logging into the miner. Defaults to "admin".
        pool_map (dict): A mapping of pool keys to `Pool` objects.
        driver (Optional[webdriver.Chrome]): The Selenium WebDriver instance for interacting
            with the miner's web interface.
    Methods:
        set_pool(pool_key: str, index: int = 1):
            Switches the miner to the specified pool.
        reboot():
            Initiates a reboot of the miner.
        _login():
            Logs into the miner's web interface. Must be implemented by subclasses.
        _goto_pool_page():
            Navigates to the pool configuration page. Must be implemented by subclasses.
        _apply_pool(pool: Pool, index: int):
            Applies the specified pool configuration. Must be implemented by subclasses.
        _save():
            Saves the current configuration. Must be implemented by subclasses.
        _do_reboot():
            Triggers a reboot of the miner. Must be implemented by subclasses.
        _set_field(*ids: str, val: str):
            Helper method to set the value of a field identified by one of the given IDs.
            Raises a `NoSuchElementException` if none of the IDs are found.
    """
    def __init__(self, cfg: Dict[str, Any]):
        self.name = cfg["name"]
        self.ip = cfg["ip"]
        self.username = cfg.get("username", "root")
        self.password = cfg.get("password", "admin")
        self.pool_map = {k: Pool(**v) for k, v in cfg.get("pools", {}).items()}
        self.driver: Optional[webdriver.Chrome] = None

    # -------------- public API --------------
    def set_pool(self, pool_key: str, index: int = 1):
        pool = self.pool_map.get(pool_key)
        if not pool:
            logger.error("%s: unknown pool key '%s'", self.name, pool_key)
            return
        logger.info("%s: switching to pool '%s'", self.name, pool_key)
        try:
            self.driver = _new_driver()
            self._login()
            self._goto_pool_page()
            self._apply_pool(pool, index)
            self._save()
            logger.info("%s: pool switch complete", self.name)
        except Exception as exc:
            logger.exception("%s: pool switch FAILED – %s", self.name, exc)
        finally:
            if self.driver:
                self.driver.quit()
                self.driver = None

    def reboot(self):
        logger.info("%s: initiating reboot", self.name)
        try:
            self.driver = _new_driver()
            self._login()
            self._do_reboot()
            logger.info("%s: reboot triggered", self.name)
        except Exception as exc:
            logger.exception("%s: reboot FAILED – %s", self.name, exc)
        finally:
            if self.driver:
                self.driver.quit()
                self.driver = None

    # ---- required implementations ----
    def _login(self):
        raise NotImplementedError

    def _goto_pool_page(self):
        raise NotImplementedError

    def _apply_pool(self, pool: Pool, index: int):
        raise NotImplementedError

    def _save(self):
        raise NotImplementedError

    def _do_reboot(self):
        raise NotImplementedError

    # helpers
    def _set_field(self, *ids: str, val: str):
        for fid in ids:
            try:
                el = self.driver.find_element(By.ID, fid)
                el.clear(); el.send_keys(val)
                return
            except NoSuchElementException:
                continue
        raise NoSuchElementException(f"none of IDs {ids} present")

# ------------------------------------------------------------------
# Antminer implementation (Vue UI)
# ------------------------------------------------------------------
class Antminer(MinerBase):
    """Bitmain S19‑series firmware (Vue, HTTP Basic‑Auth)"""

    def _login(self):
        self.driver.get(f"http://{self.username}:{self.password}@{self.ip}/")
        _wait(self.driver, 15, EC.presence_of_element_located((By.CSS_SELECTOR, "li.item[data-id='miner']")))

    def _goto_pool_page(self):
        self.driver.find_element(By.CSS_SELECTOR, "li.item[data-id='miner']").click()
        _wait(self.driver, 15, EC.presence_of_element_located((By.ID, "poolForm")))

    def _apply_pool(self, pool: Pool, index: int):
        rows = self.driver.find_elements(By.CSS_SELECTOR, "#poolTable tbody tr")
        if index < 1 or index > len(rows):
            raise NoSuchElementException(f"row {index} out of range (rows={len(rows)})")
        addr_in, name_in, pwd_in = rows[index - 1].find_elements(By.TAG_NAME, "input")[:3]
        for el, val in zip((addr_in, name_in, pwd_in), (pool.url, pool.worker, pool.password)):
            el.clear(); el.send_keys(val)

    def _save(self):
        for sel in [
            (By.CSS_SELECTOR, "input.btn-blue[data-i18n-value='save']"),
            (By.CSS_SELECTOR, "input.btn-blue[value='Save']"),
            (By.XPATH, "//input[@type='button' and contains(@value,'Save')]")]:
            try:
                self.driver.find_element(*sel).click(); break
            except NoSuchElementException:
                continue
        try:
            _wait(self.driver, 10, EC.visibility_of_element_located((By.CSS_SELECTOR, ".message.success")))
        except TimeoutException:
            pass

    def _do_reboot(self):
        # Footer button with id="restart"
        try:
            self.driver.find_element(By.ID, "restart").click()
            # A confirmation popup follows – click its confirm button (id="restartFun")
            _wait(self.driver, 10, EC.element_to_be_clickable((By.ID, "restartFun")))
            self.driver.find_element(By.ID, "restartFun").click()
        except NoSuchElementException:
            logger.error("%s: restart button not found – firmware mismatch?", self.name)

# ------------------------------------------------------------------
# Whatsminer implementation (LuCI UI)
# ------------------------------------------------------------------
class Whatsminer(MinerBase):
    """MicroBT Whatsminer (LuCI)"""
    def _login(self):
        self.driver.get(f"http://{self.ip}/cgi-bin/luci")
        _wait(self.driver, 15, EC.presence_of_element_located((By.NAME, "username")))
        self.driver.find_element(By.NAME, "username").send_keys(self.username)
        self.driver.find_element(By.NAME, "password").send_keys(self.password)
        self.driver.find_element(By.XPATH, "//button[@type='submit' or @value='Login']").click()

    def _goto_pool_page(self):
        self.driver.get(f"http://{self.ip}/cgi-bin/luci/admin/network/btminer")
        _wait(self.driver, 15, EC.presence_of_element_located((By.ID, "cbid.btminer.1.url")))

    def _apply_pool(self, pool: Pool, index: int):
        pref = f"cbid.btminer.{index}"
        fallback = f"cbid.table.{index}"
        self._set_field(f"{pref}.url", f"{fallback}.url", val=pool.url)
        self._set_field(f"{pref}.user", f"{fallback}.user", val=pool.worker)
        self._set_field(f"{pref}.pass", f"{fallback}.pass", val=pool.password)

    def _save(self):
        for sel in [
            (By.CSS_SELECTOR, "input.cbi-button-save"),
            (By.NAME, "cbi.apply"),
            (By.XPATH, "//input[@type='submit' and contains(@value,'Save')]")]:
            try:
                self.driver.find_element(*sel).click(); break
            except NoSuchElementException:
                continue
        try:
            _wait(self.driver, 15, EC.visibility_of_element_located((By.CSS_SELECTOR, ".alert-message, .cbi-progressbar")))
        except TimeoutException:
            pass

    def _do_reboot(self):
        # LuCI provides a direct restart URL
        self.driver.get(f"http://{self.ip}/cgi-bin/luci/admin/status/btminerstatus/restart")
        # If a confirm page appears, click the first form submit
        try:
            _wait(self.driver, 10, EC.element_to_be_clickable((By.XPATH, "//input[@type='submit']")))
            self.driver.find_element(By.XPATH, "//input[@type='submit']").click()
        except TimeoutException:
            pass

# ------------------------------------------------------------------
# Scheduler & CLI glue
# ------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build_miners(cfg: Dict[str, Any]) -> List[MinerBase]:
    miners: List[MinerBase] = []
    for m in cfg.get("miners", []):
        kind = m.get("type", "antminer").lower()
        if kind == "antminer":
            miners.append(Antminer(m))
        elif kind == "whatsminer":
            miners.append(Whatsminer(m))
        else:
            raise ValueError(f"unknown miner type '{kind}'")
    return miners


def schedule_jobs(miners: List[MinerBase], jobs_cfg: List[Dict[str, Any]], tz: str):
    sched = BlockingScheduler(timezone=tz)
    for j in jobs_cfg:
        trigger = CronTrigger.from_crontab(j["cron"])
        pool_key = j["pool_key"]
        def job(pk=pool_key):
            for m in miners:
                m.set_pool(pk)
        sched.add_job(job, trigger, name=f"set-{pool_key}")
        logger.info("scheduled %s -> %s", j["cron"], pool_key)
    sched.start()

# ------------------------------------------------------------------
# Entry‑point
# ------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ASIC pool switcher / rebooter (Selenium)")
    ap.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    ap.add_argument("--pool", help="Pool key to apply immediately (skips scheduler)")
    ap.add_argument("--index", type=int, default=1, help="Pool slot index (1‑3)")
    ap.add_argument("--miner", nargs="*", help="Restrict to named miners")
    ap.add_argument("--reboot", action="store_true", help="Reboot miners after switching (or standalone)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    miners = build_miners(cfg)

    if args.miner:
        miners = [m for m in miners if m.name in args.miner]
        if not miners:
            logger.error("No miners matched names: %s", args.miner)
            sys.exit(1)

    # On‑demand branch ---------------------------------------------------
    if args.pool or args.reboot:
        for m in miners:
            if args.pool:
                m.set_pool(args.pool, index=args.index)
            if args.reboot:
                m.reboot()
        sys.exit(0)

    # Scheduler branch ---------------------------------------------------
    schedule_jobs(miners, cfg.get("schedule", []), tz=cfg.get("timezone", "UTC"))
    logger.info("Scheduler started")