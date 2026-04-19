import time
import os
import sys
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ======================== 配置区域 ========================
# 需要抓取的区域列表
REGIONS = [
    {"name": "湖北", "file": "hubei4K.m3u", "group": "湖北地区"},
    # 按需添加更多区域，例如：
    # {"name": "北京", "file": "beijing.m3u", "group": "北京地区"},
]

# 区域对应的点击选择器（XPath）—— 请根据实际网页修改
AREA_SELECTORS = {
    "湖北": "//a[contains(text(), '湖北')]",
    # "北京": "//a[contains(text(), '北京')]",
}

# ======================== 辅助函数 ========================
def test_m3u8_url(url, timeout=5):
    """测试 M3U8 链接是否可用（HEAD 或 GET 前几个字节）"""
    try:
        # 尝试 HEAD 请求
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return True
        # HEAD 失败则尝试 GET 少量数据
        resp = requests.get(url, timeout=timeout, stream=True)
        for chunk in resp.iter_content(chunk_size=1024):
            if chunk:
                return True
        return False
    except Exception:
        return False

def safe_find_element(driver, by, value, timeout=10):
    """安全查找元素，找不到返回 None"""
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    except TimeoutException:
        return None

def safe_click(driver, by, value, timeout=10):
    """安全点击元素"""
    try:
        elem = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )
        elem.click()
        return True
    except TimeoutException:
        print(f"  [错误] 无法点击元素: {value}")
        return False

# ======================== 核心处理函数 ========================
def process_region(driver, region):
    """处理单个区域：点击区域 → 点击最新 IP → 获取 M3U → 格式化并测试链接 → 保存文件"""
    print(f"\n========== 开始处理区域: {region['name']} ==========")

    # 1. 点击区域标签
    selector = AREA_SELECTORS.get(region["name"])
    if not selector:
        print(f"  [跳过] 未配置区域 '{region['name']}' 的选择器")
        return

    if not safe_click(driver, By.XPATH, selector, timeout=15):
        print(f"  [失败] 点击区域 '{region['name']}' 失败")
        return
    print("  [OK] 已点击区域标签")
    time.sleep(2)  # 等待页面刷新

    # 2. 找到并点击“新上线”的第一个 IP 链接
    # 注意：以下 XPath 为示例，需根据实际页面结构调整
    first_ip_link = safe_find_element(driver, By.XPATH, "//table//tbody/tr[1]/td[1]/a", timeout=15)
    if not first_ip_link:
        print("  [失败] 未找到 IP 链接，请检查 XPath 选择器")
        return
    ip_url = first_ip_link.get_attribute("href")
    print(f"  [OK] 找到 IP 链接: {ip_url}")
    first_ip_link.click()
    time.sleep(2)

    # 3. 点击“查看频道列表”按钮
    if not safe_click(driver, By.XPATH, "//button[contains(text(), '查看频道列表')]", timeout=15):
        print("  [失败] 未找到‘查看频道列表’按钮")
        return
    print("  [OK] 已点击‘查看频道列表’")
    time.sleep(3)  # 等待列表加载

    # 4. 获取 M3U 原始内容（假设在 <pre> 标签内）
    pre_elem = safe_find_element(driver, By.XPATH, "//pre", timeout=15)
    if not pre_elem:
        print("  [失败] 未找到 M3U 内容区域 (<pre>)")
        return
    raw_content = pre_elem.text
    if not raw_content:
        print("  [警告] 获取到的 M3U 内容为空")
        return

    lines = raw_content.splitlines()
    print(f"  [OK] 获取到 {len(lines)} 行原始数据")

    # 5. 解析、测试并格式化
    final_lines = ["#EXTM3U"]
    i = 0
    valid_count = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            # 下一行应该是 URL
            if i + 1 < len(lines):
                url = lines[i+1].strip()
                # 提取频道名（最后一个逗号之后的内容）
                channel_name = line.split(",")[-1].strip()
                # 测试 URL
                if test_m3u8_url(url):
                    group_title = f'group-title="{region["group"]}"'
                    final_lines.append(f'#EXTINF:-1 {group_title},{channel_name}')
                    final_lines.append(url)
                    valid_count += 1
                    print(f"    [有效] {channel_name}")
                else:
                    print(f"    [无效] {channel_name}")
                i += 2  # 跳过 URL 行
                continue
        i += 1

    if valid_count == 0:
        print("  [警告] 未找到任何有效的频道链接，文件将不会生成")
        return

    # 6. 写入文件
    output_file = region["file"]
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))
    print(f"  [完成] 生成 {output_file}，共 {valid_count} 个有效频道")

# ======================== 主函数 ========================
def main():
    # 配置 Chrome 选项（无头模式 + 必要参数）
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument('--headless')               # 无头模式（CI 必需；本地调试可注释）
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-software-rasterizer')
    chrome_options.add_argument('--disable-setuid-sandbox')
    chrome_options.add_argument('--remote-debugging-port=9222')

    # 启动浏览器
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)   # 页面加载超时
    driver.implicitly_wait(10)          # 隐式等待

    try:
        target_url = "https://iptv.cqshushu.com/index.php?t=multicast"
        print(f"正在打开: {target_url}")
        driver.get(target_url)

        # 等待页面主体加载
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        print("页面加载完成\n")

        # 依次处理每个区域
        for region in REGIONS:
            process_region(driver, region)

        print("\n所有区域处理完毕！")
    except Exception as e:
        print(f"脚本执行出错: {e}")
        sys.exit(1)
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
