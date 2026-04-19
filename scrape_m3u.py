import time
import os
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- 配置区域 ---
# 你可以在这里添加或修改需要抓取的区域
REGIONS = [
    {"name": "湖北", "file": "hubei4K.m3u", "group": "湖北地区"},
    # 示例: {"name": "北京", "file": "beijing.m3u", "group": "北京地区"},
    # {"name": "上海", "file": "shanghai.m3u", "group": "上海地区"},
]

def test_m3u8_url(url, timeout=5):
    """测试M3U8链接是否可用"""
    try:
        # 发起HEAD请求，快速检查
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        # 如果HEAD请求失败或状态码不是2xx，尝试GET请求
        if response.status_code != 200:
            response = requests.get(url, timeout=timeout, stream=True)
            # 只读取前几个字节就断开，节省时间
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    return True
        return response.status_code == 200
    except Exception:
        return False

def process_region(driver, region):
    """处理单个区域的抓取和文件生成"""
    print(f"开始处理区域: {region['name']}")

    # 1. 选择区域（点击对应的区域标签）
    area_selectors = {
        "湖北": "//a[contains(text(), '湖北')]",
        # 你可以在这里补充其他区域的选择器
    }
    selector = area_selectors.get(region["name"])
    if not selector:
        print(f"未找到区域 '{region['name']}' 的选择器，跳过。")
        return

    area_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, selector))
    )
    area_button.click()
    time.sleep(3)  # 等待页面刷新

    # 2. 找到并点击最新上线的IP条目（假设页面表格中的第一个IP链接是新的）
    # 注意：这里的选择器需要根据实际页面结构调整
    first_ip_link = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, "//table//tbody/tr[1]/td[1]/a"))
    )
    ip_url = first_ip_link.get_attribute("href")
    first_ip_link.click()
    time.sleep(3)  # 等待新页面加载

    # 3. 在新页面中，点击"查看频道列表"按钮
    list_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '查看频道列表')]"))
    )
    list_button.click()
    time.sleep(5)  # 等待M3U列表加载完成

    # 4. 提取M3U内容并测试
    m3u_content_element = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, "//pre"))  # 假设列表在<pre>标签内
    )
    raw_m3u_lines = m3u_content_element.text.split('\n')

    # 5. 格式化并测试频道
    final_lines = ["#EXTM3U"]
    for i, line in enumerate(raw_m3u_lines):
        if line.startswith('#EXTINF:'):
            # 假设下一行是URL
            if i + 1 < len(raw_m3u_lines):
                url = raw_m3u_lines[i+1].strip()
                # 测试URL是否可用
                if test_m3u8_url(url):
                    # 格式化频道信息
                    channel_name = line.split(',')[-1].strip()
                    group_title = f'group-title="{region["group"]}"'
                    # 生成标准的M3U行
                    final_lines.append(f'#EXTINF:-1 {group_title},{channel_name}')
                    final_lines.append(url)
                    print(f"  [有效] {channel_name}")
                else:
                    print(f"  [无效] {channel_name}")
        # 跳过原URL行，因为我们已经处理过了

    # 6. 写入文件
    output_file = region["file"]
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(final_lines))
    print(f"区域 '{region['name']}' 处理完成，生成文件: {output_file}\n")

def main():
    # 设置Chrome选项，用于无头模式运行
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument('--headless')  # 无头模式，不显示浏览器窗口
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')

    # 启动浏览器驱动
    driver = webdriver.Chrome(options=chrome_options)
    driver.implicitly_wait(10)

    try:
        # 打开目标网站
        target_url = "https://iptv.cqshushu.com/index.php?t=multicast"
        print(f"正在打开网站: {target_url}")
        driver.get(target_url)

        # 等待页面初始加载完成
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(3)  # 额外等待动态内容

        # 依次处理每个区域
        for region in REGIONS:
            process_region(driver, region)

        print("所有区域处理完毕！")
    except Exception as e:
        print(f"脚本运行出错: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
