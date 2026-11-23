/*
 * tempmon.c - AX8860 监控（NPU 温度 / CPU 负载 / 内存 / CMM / llama-server）
 *
 * 单进程、单线程、poll() 多路复用，零依赖。页面 JS 每 1s 拉 /api/stats。
 * 可选主动向固定 monitor 上报板卡身份，用于 DHCP 地址变化后的自动发现。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <dirent.h>
#include <ctype.h>
#include <stdarg.h>
#include <strings.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/time.h>
#include <sys/statvfs.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <poll.h>
#include <netdb.h>
#include <pthread.h>

#define PORT_DEFAULT 8080
#define MAX_ZONES 16
#define MAX_BUF 32768
#define AGENT_VERSION "2.2.0"
#define LLAMA_PORT_MIN 18080
#define LLAMA_PORT_MAX 18089

static char zones[MAX_ZONES][300];
static int  nzones = 0;
static char bind_address[64] = "0.0.0.0";
static char auth_token[256] = "";
static char report_url[512] = "";
static char report_name[128] = "";
static char report_token[256] = "";
static int report_interval = 10;
static int listen_port = PORT_DEFAULT;

static void json_append(char **p, size_t *left, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

static void scan_zones(void) {
    DIR *d = opendir("/sys/class/thermal");
    struct dirent *e;
    nzones = 0;
    if (!d) return;
    while ((e = readdir(d)) && nzones < MAX_ZONES) {
        if (strncmp(e->d_name, "thermal_zone", 12) != 0) continue;
        snprintf(zones[nzones], sizeof(zones[nzones]),
                 "/sys/class/thermal/%.*s",
                 (int)(sizeof(zones[nzones]) - 24), e->d_name);
        nzones++;
    }
    closedir(d);
}

static long read_long_file(const char *path) {
    char buf[64];
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strtol(buf, NULL, 10);
}

static void read_str_file(const char *path, char *out, size_t n) {
    int fd = open(path, O_RDONLY);
    out[0] = 0;
    if (fd < 0) return;
    ssize_t r = read(fd, out, n - 1);
    if (r > 0) {
        out[r] = 0;
        char *p = strchr(out, '\n');
        if (p) *p = 0;
    }
    close(fd);
}

/* ---- 数据采集 ---- */

/* CPU loadavg: 1/5/15 */
static void get_load(float *l1, float *l5, float *l15) {
    char buf[128];
    read_str_file("/proc/loadavg", buf, sizeof(buf));
    *l1 = *l5 = *l15 = 0;
    sscanf(buf, "%f %f %f", l1, l5, l15);
}

/* 内存: 返回 total/avail KB */
static void get_mem(long *total_kb, long *avail_kb) {
    FILE *f = fopen("/proc/meminfo", "r");
    char key[64];
    long val;
    *total_kb = *avail_kb = 0;
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "%63s %ld", key, &val) == 2) {
            if (strcmp(key, "MemTotal:") == 0) *total_kb = val;
            else if (strcmp(key, "MemAvailable:") == 0) *avail_kb = val;
        }
    }
    fclose(f);
}

/* 读取每个 CPU core 的频率 MHz，返回核数 */
static int get_cpu_freqs(long mhz[], int max_cores) {
    int n = 0;
    for (int i = 0; i < max_cores && i < 64; i++) {
        char path[128];
        snprintf(path, sizeof(path),
                 "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq", i);
        long v = read_long_file(path);
        if (v <= 0) continue;      /* 该核不存在或无 cpufreq */
        mhz[n++] = v / 1000;
    }
    return n;
}

static long get_tdp_freq_mhz(void) {
    FILE *f = fopen("/proc/ax_proc/tdp", "r");
    if (!f) return 0;
    char line[512];
    long value = 0;
    while (fgets(line, sizeof(line), f)) {
        char *p = strstr(line, "cur freq(MHz)");
        if (!p) continue;
        p = strstr(p, ":");
        if (p) {
            value = strtol(p + 1, NULL, 10);
            break;
        }
    }
    fclose(f);
    return value;
}

static int get_npu_clocks(long mhz[], int max_cores) {
    int n = 0;
    for (int i = 0; i < max_cores && i < 8; i++) {
        char path[192];
        snprintf(path, sizeof(path),
                 "/sys/kernel/debug/clk/npu%d_clk_nn_glb/clk_rate", i);
        long hz = read_long_file(path);
        if (hz > 0) mhz[n++] = hz / 1000000;
    }
    return n;
}

/* NPU 利用率：读取 /proc/ax_proc/npu/top，对所有 core/vNPU 行取平均。
 * 返回 -1 表示统计未使能或接口不可用。 */
static double get_npu_util_avg(void) {
    FILE *f = fopen("/proc/ax_proc/npu/top", "r");
    if (!f) return -1;
    char line[512];
    int count = 0;
    double sum = 0.0;
    while (fgets(line, sizeof(line), f)) {
        int cluster, core, vnpu, util;
        long times, period;
        if (sscanf(line, "%d %d %d %ld %ld %d%%",
                   &cluster, &core, &vnpu, &times, &period, &util) == 6) {
            sum += util;
            count++;
        }
    }
    fclose(f);
    return count ? sum / count : -1;
}

static int get_llama_processes(char *js, size_t cap, long *rss_total_kb) {
    DIR *d = opendir("/proc");
    struct dirent *e;
    char *p = js;
    size_t left = cap;
    int count = 0;
    *rss_total_kb = 0;
    json_append(&p, &left, "[");
    if (d) {
        while ((e = readdir(d))) {
            int digit = *e->d_name != 0;
            for (const char *s = e->d_name; *s; s++) if (!isdigit((unsigned char)*s)) { digit = 0; break; }
            if (!digit) continue;

            char path[280], comm[128];
            snprintf(path, sizeof(path), "/proc/%s/comm", e->d_name);
            read_str_file(path, comm, sizeof(comm));
            if (!comm[0] || strstr(comm, "llama") == NULL) continue;

            long rss_kb = 0;
            snprintf(path, sizeof(path), "/proc/%s/status", e->d_name);
            FILE *f = fopen(path, "r");
            if (f) {
                char line[256];
                while (fgets(line, sizeof(line), f)) {
                    if (strncmp(line, "VmRSS:", 6) == 0) {
                        rss_kb = strtol(line + 6, NULL, 10);
                        break;
                    }
                }
                fclose(f);
            }
            *rss_total_kb += rss_kb;
            json_append(&p, &left, "%s{\"pid\":%ld,\"rss_kb\":%ld}",
                        count ? "," : "", strtol(e->d_name, NULL, 10), rss_kb);
            count++;
        }
        closedir(d);
    }
    json_append(&p, &left, "]");
    return count;
}

static int get_llama_listen_ports(char *js, size_t cap) {
    static const char *files[] = {"/proc/net/tcp", "/proc/net/tcp6", NULL};
    int ports[32], count = 0;
    char *p = js;
    size_t left = cap;

    for (int fi = 0; files[fi]; fi++) {
        FILE *f = fopen(files[fi], "r");
        if (!f) continue;
        char line[512];
        while (fgets(line, sizeof(line), f)) {
            unsigned long local_addr, local_port, remote_addr, remote_port, state;
            if (sscanf(line, "%*u: %lx:%lx %lx:%lx %lx",
                       &local_addr, &local_port, &remote_addr, &remote_port, &state) != 5) continue;
            if (state != 10) continue;               /* TCP_LISTEN */
            if (local_port < LLAMA_PORT_MIN || local_port > LLAMA_PORT_MAX) continue;

            int duplicate = 0;
            for (int i = 0; i < count; i++) if (ports[i] == (int)local_port) { duplicate = 1; break; }
            if (duplicate) continue;
            if (count < (int)(sizeof(ports) / sizeof(ports[0]))) ports[count++] = (int)local_port;
        }
        fclose(f);
    }

    json_append(&p, &left, "[");
    for (int i = 0; i < count; i++) json_append(&p, &left, "%s%d", i ? "," : "", ports[i]);
    json_append(&p, &left, "]");
    return count;
}

/* ---- JSON ---- */

static void json_append(char **p, size_t *left, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

static void json_append(char **p, size_t *left, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(*p, *left, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n >= *left) n = (int)(*left - 1);
    *p += n; *left -= n;
}

static void json_append_string(char **p, size_t *left, const char *value) {
    json_append(p, left, "\"");
    for (const unsigned char *s = (const unsigned char *)(value ? value : ""); *s; s++) {
        switch (*s) {
            case '"': json_append(p, left, "\\\""); break;
            case '\\': json_append(p, left, "\\\\"); break;
            case '\b': json_append(p, left, "\\b"); break;
            case '\f': json_append(p, left, "\\f"); break;
            case '\n': json_append(p, left, "\\n"); break;
            case '\r': json_append(p, left, "\\r"); break;
            case '\t': json_append(p, left, "\\t"); break;
            default:
                if (*s < 0x20) json_append(p, left, "\\u%04x", *s);
                else json_append(p, left, "%c", *s);
        }
    }
    json_append(p, left, "\"");
}

static int send_all(int fd, const char *data, size_t length) {
    size_t sent = 0;
    while (sent < length) {
        ssize_t n = send(fd, data + sent, length - sent, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        sent += (size_t)n;
    }
    return 0;
}

static int report_once(void) {
    char url[sizeof(report_url)];
    char host[256] = "", path[256] = "/api/report";
    int port = 80;
    const char *scheme = "http://";

    snprintf(url, sizeof(url), "%s", report_url);
    char *p = url;
    if (strncmp(p, scheme, strlen(scheme)) != 0) return -1;
    p += strlen(scheme);

    char *slash = strchr(p, '/');
    if (slash) {
        size_t path_len = strlen(slash);
        if (path_len >= sizeof(path)) path_len = sizeof(path) - 1;
        memcpy(path, slash, path_len);
        path[path_len] = 0;
        *slash = 0;
    }

    char *colon = strchr(p, ':');
    if (colon) {
        long value = strtol(colon + 1, NULL, 10);
        if (value <= 0 || value > 65535) return -1;
        port = (int)value;
        *colon = 0;
    }
    size_t host_len = strlen(p);
    if (host_len == 0 || host_len >= sizeof(host)) return -1;
    memcpy(host, p, host_len);
    host[host_len] = 0;

    struct addrinfo hints, *result = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    if (getaddrinfo(host, NULL, &hints, &result) != 0 || !result) return -1;
    ((struct sockaddr_in *)result->ai_addr)->sin_port = htons((uint16_t)port);

    int fd = socket(result->ai_family, result->ai_socktype, result->ai_protocol);
    if (fd < 0) {
        freeaddrinfo(result);
        return -1;
    }
    struct timeval timeout = {2, 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));

    char payload[512];
    char *out = payload;
    size_t left = sizeof(payload);
    json_append(&out, &left, "{\"name\":");
    json_append_string(&out, &left, report_name);
    json_append(&out, &left, ",\"port\":%d,\"agent_version\":\"" AGENT_VERSION "\"}", listen_port);

    char request[1536];
    int request_len = snprintf(
        request, sizeof(request),
        "POST %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "User-Agent: tempmon/" AGENT_VERSION "\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %zu\r\n"
        "Connection: close\r\n"
        "%s%s%s"
        "\r\n%s",
        path, host, port, strlen(payload),
        report_token[0] ? "Authorization: Bearer " : "",
        report_token[0] ? report_token : "",
        report_token[0] ? "\r\n" : "",
        payload);
    if (request_len < 0 || (size_t)request_len >= sizeof(request) ||
        connect(fd, result->ai_addr, result->ai_addrlen) < 0 ||
        send_all(fd, request, (size_t)request_len) < 0) {
        close(fd);
        freeaddrinfo(result);
        return -1;
    }
    freeaddrinfo(result);

    char response[512] = "";
    ssize_t received;
    do {
        received = recv(fd, response, sizeof(response) - 1, 0);
    } while (received < 0 && errno == EINTR);
    close(fd);
    if (received <= 0) return -1;
    response[received] = 0;
    char *status = strchr(response, ' ');
    return status && strtol(status + 1, NULL, 10) == 200 ? 0 : -1;
}

static void *report_thread(void *arg) {
    (void)arg;
    int last_result = -1;
    for (;;) {
        int result = report_once();
        if (result != last_result) {
            fprintf(stderr,
                    "tempmon report %s: %s\n",
                    result == 0 ? "connected" : "failed",
                    report_url);
            last_result = result;
        }
        sleep((unsigned int)report_interval);
    }
    return NULL;
}

/* ---- CMM（NPU 内存池）----
 * 解析 /proc/ax_proc/mem_cmm_info：
 *   +---PARTITION: ... Size=NNNNKB(...), NAME="..."
 *    nBlock(...)  nbytes(... Cur=NNNNB(...) ...)
 * 汇总所有 partition 的 Size 与 Cur。
 */
static void get_cmm(long *total_kb, long *used_kb) {
    *total_kb = 0; *used_kb = 0;
    FILE *f = fopen("/proc/ax_proc/mem_cmm_info", "r");
    if (!f) return;
    char line[1024];
    /* 解析最后一行 CMM_USE_INFO 汇总：
     * total size=50331648KB(49152MB),used=27522532KB(26877MB + 484KB),remain=...
     */
    while (fgets(line, sizeof(line), f)) {
        char *ts = strstr(line, "total size=");
        if (ts) {
            *total_kb = strtol(ts + 11, NULL, 10);
            char *us = strstr(line, "used=");
            if (us) *used_kb = strtol(us + 5, NULL, 10);
        }
    }
    fclose(f);
}

/* ---- 磁盘（排除 NFS / 伪文件系统）---- */
static int is_ignored_fs(const char *t) {
    static const char *excl[] = {
        "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "overlay",
        "squashfs", "ramfs", "debugfs", "tracefs", "securityfs", "pstore",
        "bpf", "autofs", "mqueue", "hugetlbfs", "configfs", "binfmt_misc",
        "efivarfs", "fusectl", "devpts", "selinuxfs", "nsfs", NULL
    };
    if (strncmp(t, "nfs", 3) == 0) return 1;     /* NFS/NFS4 */
    if (strncmp(t, "cifs", 4) == 0) return 1;
    if (strncmp(t, "smb", 3) == 0) return 1;
    if (strncmp(t, "fuse", 4) == 0) return 1;    /* sshfs/rclone 等 */
    for (int i = 0; excl[i]; i++) if (strcmp(t, excl[i]) == 0) return 1;
    return 0;
}

/* 返回磁盘数量，并把 JSON 数组写入 js */
static int get_disks(char *js, size_t cap) {
    FILE *f = fopen("/proc/mounts", "r");
    if (!f) { snprintf(js, cap, "[]"); return 0; }
    char dev[256], mnt[256], type[64];
    char seen[32][256];
    int nseen = 0, count = 0;
    char *p = js; size_t left = cap;
    json_append(&p, &left, "[");
    while (fscanf(f, "%255s %255s %63s %*s %*s %*s", dev, mnt, type) == 3) {
        if (is_ignored_fs(type)) continue;
        int dup = 0;
        for (int i = 0; i < nseen; i++) if (strcmp(seen[i], dev) == 0) { dup = 1; break; }
        if (dup) continue;
        if (nseen < 32) {
            size_t dev_len = strnlen(dev, 255);
            memcpy(seen[nseen], dev, dev_len);
            seen[nseen][dev_len] = 0;
            nseen++;
        }
        struct statvfs st;
        if (statvfs(mnt, &st) != 0) continue;
        unsigned long long total = (unsigned long long)st.f_blocks * st.f_frsize / 1024;
        unsigned long long avail = (unsigned long long)st.f_bavail * st.f_frsize / 1024;
        if (total == 0) continue;
        unsigned long long used = (total > avail) ? total - avail : 0;
        json_append(&p, &left,
                    "%s{\"mount\":\"%s\",\"fs\":\"%s\",\"total_kb\":%llu,\"used_kb\":%llu,\"avail_kb\":%llu}",
                    count ? "," : "", mnt, type, total, used, avail);
        count++;
    }
    fclose(f);
    json_append(&p, &left, "]");
    return count;
}

static void build_json(char *buf, size_t cap) {
    char *p = buf; size_t left = cap;
    float l1, l5, l15;
    long total_kb, avail_kb;
    long cmm_total_kb, cmm_used_kb;
    long cpu_freqs[64];
    int ncores;
    char disks_js[8192];
    char llama_js[4096], listen_js[1024];
    long tdp_mhz, npu_clocks[8], llama_rss_kb = 0;
    double npu_util_avg;
    int nnpu, llama_procs, llama_ports;
    char bsp_version[128], chip_type[128];
    /* 网络速率：读 end0 累计字节数，跟上一次采样做差 */
    static long prev_rx = 0, prev_tx = 0;
    static time_t prev_t = 0;
    long rx_bytes = read_long_file("/sys/class/net/end0/statistics/rx_bytes");
    long tx_bytes = read_long_file("/sys/class/net/end0/statistics/tx_bytes");
    time_t now = time(NULL);
    double rx_mbps = 0, tx_mbps = 0;
    if (prev_t > 0 && now > prev_t && rx_bytes >= prev_rx && tx_bytes >= prev_tx) {
        double dt = (double)(now - prev_t);
        rx_mbps = (rx_bytes - prev_rx) * 8.0 / dt / 1e6;
        tx_mbps = (tx_bytes - prev_tx) * 8.0 / dt / 1e6;
    }
    prev_rx = rx_bytes; prev_tx = tx_bytes; prev_t = now;
    get_load(&l1, &l5, &l15);
    get_mem(&total_kb, &avail_kb);
    get_cmm(&cmm_total_kb, &cmm_used_kb);
    ncores = get_cpu_freqs(cpu_freqs, 64);
    get_disks(disks_js, sizeof(disks_js));
    tdp_mhz = get_tdp_freq_mhz();
    nnpu = get_npu_clocks(npu_clocks, 8);
    npu_util_avg = get_npu_util_avg();
    llama_procs = get_llama_processes(llama_js, sizeof(llama_js), &llama_rss_kb);
    llama_ports = get_llama_listen_ports(listen_js, sizeof(listen_js));
    read_str_file("/proc/ax_proc/version", bsp_version, sizeof(bsp_version));
    read_str_file("/proc/ax_proc/chip_type", chip_type, sizeof(chip_type));

    json_append(&p, &left, "{\"time\":%lld", (long long)time(NULL));
    json_append(&p, &left, ",\"agent\":{\"version\":\"" AGENT_VERSION "\",\"bsp_version\":");
    json_append_string(&p, &left, bsp_version);
    json_append(&p, &left, ",\"chip_type\":");
    json_append_string(&p, &left, chip_type);
    json_append(&p, &left, "}");
    json_append(&p, &left, ",\"load\":{\"1\":%.2f,\"5\":%.2f,\"15\":%.2f}", l1, l5, l15);
    json_append(&p, &left, ",\"mem\":{\"total_kb\":%ld,\"avail_kb\":%ld,\"used_kb\":%ld}",
                total_kb, avail_kb, total_kb - avail_kb);
    json_append(&p, &left, ",\"cmm\":{\"total_kb\":%ld,\"used_kb\":%ld}", cmm_total_kb, cmm_used_kb);
    json_append(&p, &left, ",\"net\":{\"iface\":\"end0\",\"rx_bytes\":%ld,\"tx_bytes\":%ld,\"rx_mbps\":%.1f,\"tx_mbps\":%.1f}",
                rx_bytes, tx_bytes, rx_mbps, tx_mbps);
    json_append(&p, &left, ",\"disks\":%s", disks_js);
    json_append(&p, &left, ",\"cpu_cores\":%d,\"cpu_freqs\":[", ncores);
    for (int i = 0; i < ncores; i++)
        json_append(&p, &left, "%s%ld", i ? "," : "", cpu_freqs[i]);
    json_append(&p, &left, "]");
    json_append(&p, &left, ",\"npu\":{\"tdp_freq_mhz\":%ld,\"clock_mhz\":[", tdp_mhz);
    for (int i = 0; i < nnpu; i++)
        json_append(&p, &left, "%s%ld", i ? "," : "", npu_clocks[i]);
    json_append(&p, &left, "],\"util_avg\":");
    if (npu_util_avg >= 0)
        json_append(&p, &left, "%.1f", npu_util_avg);
    else
        json_append(&p, &left, "null");
    json_append(&p, &left, "}");
    json_append(&p, &left, ",\"llama\":{\"process_count\":%d,\"rss_kb\":%ld,\"listening_port_count\":%d,\"processes\":%s,\"listening_ports\":%s}",
                llama_procs, llama_rss_kb, llama_ports, llama_js, listen_js);

    json_append(&p, &left, ",\"zones\":[");
    for (int i = 0; i < nzones; i++) {
        char tpath[512], tp[512], typ[128];
        snprintf(tpath, sizeof(tpath), "%.*s/temp",
                 (int)(sizeof(tpath) - 16), zones[i]);
        snprintf(tp, sizeof(tp), "%.*s/type",
                 (int)(sizeof(tp) - 16), zones[i]);
        long t = read_long_file(tpath);
        read_str_file(tp, typ, sizeof(typ));
        const char *base = strrchr(zones[i], '/');
        base = base ? base + 1 : zones[i];
        json_append(&p, &left, "%s{\"name\":\"%s\",\"type\":\"%s\",\"milli\":%ld,\"c\":%.1f}",
                    i ? "," : "", base, typ, t, t / 1000.0);
    }
    json_append(&p, &left, "]}");
}

/* ---- 页面 ---- */

static const char *HTML =
"<!doctype html><html><head><meta charset=utf-8>"
"<meta name=viewport content='width=device-width,initial-scale=1'>"
"<title>AX8860 Monitor</title>"
"<style>body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0d1117;"
"color:#c9d1d9;margin:0;padding:16px}h1{font-size:16px;margin:0 0 4px}"
"#ts{color:#8b949e;font-size:11px;margin-bottom:12px}"
".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}"
".card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px}"
".card .t{color:#8b949e;font-size:11px;margin-bottom:6px}"
".card .v{font-size:20px;font-weight:600}"
".bar{background:#21262d;border-radius:4px;height:8px;margin-top:6px;overflow:hidden}"
".fill{height:100%;background:#3fb950}"
".warn{color:#d29922}.hot{color:#f85149}.dim{color:#8b949e;font-size:11px}"
"</style></head><body>"
"<h1>AX8860 Monitor</h1><div id=ts></div>"
"<div class=grid id=g>loading...</div>"
"<script>"
"function el(html){return '<div class=card><div class=t>'+html+'</div>'}"
"function card(t,v,sub,cls){return '<div class=card><div class=t>'+t+'</div>'"
"  +'<div class=\"v '+(cls||'')+'\">'+v+'</div>'+(sub?'<div class=dim>'+sub+'</div>':'')+'</div>'}"
"function bar(pct){return '<div class=bar><div class=fill style=\"width:'+pct+'%\"></div></div>'}"
"function fmtG(kb){var g=kb/1024/1024;return g.toFixed(1)+' GB'}"
"function upd(){fetch('/api/stats').then(r=>r.json()).then(d=>{"
"var now=new Date(d.time*1000).toLocaleTimeString();"
"document.getElementById('ts').textContent='updated: '+now;"
"var used=d.mem.total_kb-d.mem.avail_kb;"
"var mpct=Math.min(100,used/d.mem.total_kb*100);"
"var h='';"
"h+=card('CPU Load (1/5/15)', d.load['1'].toFixed(2)+' / '+d.load['5'].toFixed(2)+' / '+d.load['15'].toFixed(2), '');"
"var cmin=Math.min.apply(null,d.cpu_freqs),cmax=Math.max.apply(null,d.cpu_freqs);"
"var cf=(cmin==cmax)?(cmin/1000).toFixed(2)+' GHz':(cmin/1000).toFixed(2)+'~'+(cmax/1000).toFixed(2)+' GHz';"
"h+=card('CPU', d.cpu_cores+' cores', cf);"
"h+=card('Memory', fmtG(used)+' / '+fmtG(d.mem.total_kb), bar(mpct)+d.mem.used_kb+' KB used');"
"if(d.agent){h+=card('Agent', d.agent.version, d.agent.chip_type+' · '+d.agent.bsp_version);}"
"if(d.npu){var nu=d.npu.util_avg;h+=card('NPU Utilization', nu==null?'--':nu.toFixed(1)+'%', d.npu.tdp_freq_mhz+' MHz TDP · '+d.npu.clock_mhz.join(' / ')+' MHz clocks', nu==null?'':(nu>=90?'hot':(nu>=70?'warn':'')));}"
"if(d.llama){h+=card('llama-server', d.llama.process_count+' proc', fmtG(d.llama.rss_kb)+' RSS · ports '+d.llama.listening_ports.join(','));}"
"if(d.cmm&&d.cmm.total_kb>0){var cpct=Math.min(100,d.cmm.used_kb/d.cmm.total_kb*100);"
"var ccls=cpct>=60?'hot':(cpct>=30?'warn':'');"
"h+=card('CMM (NPU mem)', fmtG(d.cmm.used_kb)+' / '+fmtG(d.cmm.total_kb), bar(cpct)+cpct.toFixed(1)+'% used', ccls);}"
"if(d.disks)d.disks.forEach(function(dk){var pct=dk.total_kb?Math.min(100,dk.used_kb/dk.total_kb*100):0;"
"var cls=pct>=85?'hot':(pct>=70?'warn':'');"
"h+=card('Disk '+dk.mount, fmtG(dk.used_kb)+' / '+fmtG(dk.total_kb), bar(pct)+pct.toFixed(1)+'% · free '+fmtG(dk.avail_kb), cls);});"
"d.zones.forEach(z=>{var cls=z.c>=70?'hot':(z.c>=55?'warn':'');"
"h+=card(z.type, z.c.toFixed(1)+' &deg;C', bar(Math.min(100,z.c))+(z.c>=55?'HIGH':'OK'), cls)});"
"document.getElementById('g').innerHTML=h;});}"
"upd();setInterval(upd,1000);"
"</script></body></html>";

/* ---- HTTP ---- */

static void send_response(int fd, const char *status, const char *ctype,
                          const char *body, size_t len) {
    char hdr[512];
    int n = snprintf(hdr, sizeof(hdr),
        "HTTP/1.1 %s\r\nContent-Type: %s\r\n"
        "Content-Length: %zu\r\nCache-Control: no-store\r\n"
        "Connection: close\r\n\r\n", status, ctype, len);
    if (write(fd, hdr, n) < 0) return;
    if (write(fd, body, len) < 0) return;
}

static void handle(int fd) {
    char req[MAX_BUF];
    ssize_t n = read(fd, req, sizeof(req) - 1);
    if (n <= 0) return;
    req[n] = 0;

    if (auth_token[0] != 0) {
        char expected[320];
        int ok = 0;
        snprintf(expected, sizeof(expected), "Authorization: Bearer %s", auth_token);
        for (char *line = strtok(req, "\r\n"); line; line = strtok(NULL, "\r\n")) {
            if (strcasecmp(line, expected) == 0) { ok = 1; break; }
        }
        if (!ok && strncmp(req, "GET /api/stats?token=", 22) == 0) {
            char provided[256] = "";
            sscanf(req + 22, "%255[^& ]", provided);
            if (strcmp(provided, auth_token) == 0) ok = 1;
        }
        if (!ok) {
            const char *body = "{\"error\":\"unauthorized\"}";
            send_response(fd, "401 Unauthorized", "application/json", body, strlen(body));
            return;
        }
    }

    if (strncmp(req, "GET /api/stats", 14) == 0 ||
        strncmp(req, "GET /api/temp", 13) == 0) {
        char buf[MAX_BUF];
        build_json(buf, sizeof(buf));
        send_response(fd, "200 OK", "application/json", buf, strlen(buf));
    } else if (strncmp(req, "GET /health", 11) == 0) {
        const char *body = "{\"status\":\"ok\"}";
        send_response(fd, "200 OK", "application/json", body, strlen(body));
    } else if (strncmp(req, "GET /", 5) == 0 || strncmp(req, "GET ", 4) == 0) {
        send_response(fd, "200 OK", "text/html; charset=utf-8", HTML, strlen(HTML));
    } else {
        const char *nf = "not found";
        send_response(fd, "404 Not Found", "text/plain", nf, strlen(nf));
    }
}

int main(int argc, char **argv) {
    int port = PORT_DEFAULT;
    int positional_port = 0;
    const char *env = getenv("TEMPMON_PORT");
    if (env && atoi(env) > 0) port = atoi(env);
    env = getenv("TEMPMON_BIND");
    if (env && env[0]) snprintf(bind_address, sizeof(bind_address), "%s", env);
    env = getenv("TEMPMON_TOKEN");
    if (env) snprintf(auth_token, sizeof(auth_token), "%s", env);
    env = getenv("TEMPMON_REPORT_URL");
    if (env) snprintf(report_url, sizeof(report_url), "%s", env);
    env = getenv("TEMPMON_BOARD_NAME");
    if (env) snprintf(report_name, sizeof(report_name), "%s", env);
    env = getenv("TEMPMON_REPORT_TOKEN");
    if (env) snprintf(report_token, sizeof(report_token), "%s", env);
    env = getenv("TEMPMON_REPORT_INTERVAL");
    if (env) {
        int value = atoi(env);
        if (value >= 3 && value <= 3600) report_interval = value;
    }

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--bind") == 0 && i + 1 < argc) {
            snprintf(bind_address, sizeof(bind_address), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--token") == 0 && i + 1 < argc) {
            snprintf(auth_token, sizeof(auth_token), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--report-url") == 0 && i + 1 < argc) {
            snprintf(report_url, sizeof(report_url), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--board-name") == 0 && i + 1 < argc) {
            snprintf(report_name, sizeof(report_name), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--report-token") == 0 && i + 1 < argc) {
            snprintf(report_token, sizeof(report_token), "%s", argv[++i]);
        } else if (strcmp(argv[i], "--report-interval") == 0 && i + 1 < argc) {
            int value = atoi(argv[++i]);
            if (value >= 3 && value <= 3600) report_interval = value;
        } else if (!positional_port && atoi(argv[i]) > 0) {
            port = atoi(argv[i]);
            positional_port = 1;
        }
    }
    listen_port = port;

    scan_zones();
    if (nzones == 0) {
        fprintf(stderr, "no thermal zones found\n");
        return 1;
    }

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    if (inet_pton(AF_INET, bind_address, &sa.sin_addr) != 1) {
        fprintf(stderr, "invalid bind address: %s\n", bind_address);
        return 1;
    }
    sa.sin_port = htons(port);
    if (bind(srv, (struct sockaddr *)&sa, sizeof(sa)) < 0 ||
        listen(srv, 16) < 0) {
        perror("bind/listen");
        return 1;
    }
    fprintf(stderr, "tempmon %s listening on %s:%d (%d thermal zones%s)\n",
            AGENT_VERSION, bind_address, port, nzones,
            auth_token[0] ? ", bearer token enabled" : "");
    if (report_url[0]) {
        if (!report_name[0]) {
            fprintf(stderr, "report URL is set but TEMPMON_BOARD_NAME is empty\n");
        } else {
            pthread_t thread;
            if (pthread_create(&thread, NULL, report_thread, NULL) == 0) {
                pthread_detach(thread);
                fprintf(stderr,
                        "tempmon report enabled: %s as %s every %ds\n",
                        report_url, report_name, report_interval);
            } else {
                fprintf(stderr, "create report thread failed\n");
            }
        }
    }

    struct pollfd fds[1];
    for (;;) {
        fds[0].fd = srv; fds[0].events = POLLIN;
        int r = poll(fds, 1, 1000);
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) continue;
        if (fds[0].revents & POLLIN) {
            int c = accept(srv, NULL, NULL);
            if (c >= 0) {
                struct timeval tv = {2, 0};
                setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
                handle(c);
                close(c);
            }
        }
    }
    return 0;
}
