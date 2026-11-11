import re

p = 'src/backend.h'
content = open(p, 'r', encoding='utf-8').read()

old = """static int backend_cpu_n_threads(void) {
    int n = (int) std::thread::hardware_concurrency() / 2;
    return n > 0 ? n : 1;
}"""

new = """static int backend_cpu_n_threads(void) {
    if (const char * env = std::getenv("GGML_NUM_THREADS")) { int val = std::atoi(env); if (val > 0) return val; }
    if (const char * env = std::getenv("OMP_NUM_THREADS")) { int val = std::atoi(env); if (val > 0) return val; }
    int n = (int) std::thread::hardware_concurrency() / 2;
    return n > 0 ? n : 1;
}"""

if old in content:
    open(p, 'w', encoding='utf-8').write(content.replace(old, new))
    print("Replaced exact string!")
else:
    content = re.sub(
        r'static int backend_cpu_n_threads\(void\)\s*\{\s*int n = \(int\) std::thread::hardware_concurrency\(\) / 2;\s*return n > 0 \? n : 1;\s*\}',
        new,
        content
    )
    open(p, 'w', encoding='utf-8').write(content)
    print("Replaced via regex!")
