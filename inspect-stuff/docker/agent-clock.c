/* Linux libc clock interposition. Kernel waits and CPU clocks stay real. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/file.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static int adjusted_clock(clockid_t id, struct timespec *value) {
    int saved_errno = errno;
    const char *path = getenv("AGENT_CLOCK_FILE");
    int fd = path && *path ? open(path, O_RDONLY | O_CLOEXEC) : -1;
    int locked = fd >= 0 && flock(fd, LOCK_SH) == 0;
    long long excluded = 0, started = 0;
    if (locked) {
        char buffer[96];
        ssize_t size = read(fd, buffer, sizeof(buffer) - 1);
        if (size > 0) {
            buffer[size] = '\0';
            if (sscanf(buffer, "%lld %lld", &excluded, &started) != 2)
                excluded = started = 0;
        }
    }
    struct timespec now;
    int result = syscall(SYS_clock_gettime, id, value);
    int call_errno = errno;
    if (result == 0 && locked && excluded >= 0 && started >= 0 &&
        syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &now) == 0) {
        if (id == CLOCK_MONOTONIC) *value = now;
        long long real_ns = (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
        if (started && real_ns > started) excluded += real_ns - started;
        long long ns = (long long)value->tv_sec * 1000000000LL + value->tv_nsec;
        ns -= excluded;
        value->tv_sec = ns / 1000000000LL;
        value->tv_nsec = ns % 1000000000LL;
        if (value->tv_nsec < 0) {
            value->tv_sec--;
            value->tv_nsec += 1000000000L;
        }
    }
    if (fd >= 0) close(fd);
    errno = result == 0 ? saved_errno : call_errno;
    return result;
}

int clock_gettime(clockid_t id, struct timespec *value) {
    switch (id) {
        case CLOCK_REALTIME:
        case CLOCK_MONOTONIC:
        case CLOCK_BOOTTIME:
        case CLOCK_MONOTONIC_RAW:
            return adjusted_clock(id, value);
        /* Use full-resolution clocks to avoid coarse tick regressions. */
        case CLOCK_REALTIME_COARSE:
            return adjusted_clock(CLOCK_REALTIME, value);
        case CLOCK_MONOTONIC_COARSE:
            return adjusted_clock(CLOCK_MONOTONIC, value);
        default:
            return syscall(SYS_clock_gettime, id, value);
    }
}

int gettimeofday(struct timeval *value, void *zone) {
    (void)zone;
    struct timespec now;
    int result = adjusted_clock(CLOCK_REALTIME, &now);
    if (result == 0) {
        value->tv_sec = now.tv_sec;
        value->tv_usec = now.tv_nsec / 1000;
    }
    return result;
}

time_t time(time_t *out) {
    struct timespec now;
    if (adjusted_clock(CLOCK_REALTIME, &now) != 0) return (time_t)-1;
    if (out) *out = now.tv_sec;
    return now.tv_sec;
}
