/* Raw UART HCI test v2: explicitly nuke ALL special chars, test EOT pass-through */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    const char *dev = argc > 1 ? argv[1] : "/dev/ttyAMA10";

    /* Open with O_NOCTTY to avoid becoming controlling terminal */
    int fd = open(dev, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0) { fprintf(stderr, "open %s: %s\n", dev, strerror(errno)); return 1; }

    /* Clear O_NONBLOCK after open */
    int flags = fcntl(fd, F_GETFL);
    fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);

    /* Flush everything FIRST */
    tcflush(fd, TCIOFLUSH);

    struct termios ti;
    memset(&ti, 0, sizeof(ti));  /* zero everything first */
    tcgetattr(fd, &ti);

    /* Manually set raw mode - don't trust cfmakeraw alone */
    ti.c_iflag &= ~(IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL | IXON | IXOFF | IXANY | IMAXBEL);
    ti.c_oflag &= ~OPOST;
    ti.c_lflag &= ~(ECHO | ECHONL | ICANON | ISIG | IEXTEN);
    ti.c_cflag &= ~(CSIZE | PARENB | CRTSCTS);
    ti.c_cflag |= CS8 | CLOCAL | CREAD;

    /* Kill ALL special characters */
    for (int i = 0; i < NCCS; i++)
        ti.c_cc[i] = 0;
    ti.c_cc[VMIN] = 1;
    ti.c_cc[VTIME] = 0;

    cfsetispeed(&ti, B115200);
    cfsetospeed(&ti, B115200);

    if (tcsetattr(fd, TCSAFLUSH, &ti) < 0) {
        fprintf(stderr, "tcsetattr: %s\n", strerror(errno));
        close(fd); return 1;
    }
    tcflush(fd, TCIOFLUSH);
    usleep(100000); /* 100ms settle */

    /* Verify settings */
    struct termios verify;
    tcgetattr(fd, &verify);
    fprintf(stderr, "iflag=%08x oflag=%08x cflag=%08x lflag=%08x\n",
            verify.c_iflag, verify.c_oflag, verify.c_cflag, verify.c_lflag);
    fprintf(stderr, "VEOF=%d VMIN=%d VTIME=%d ICANON=%s\n",
            verify.c_cc[VEOF], verify.c_cc[VMIN], verify.c_cc[VTIME],
            (verify.c_lflag & ICANON) ? "ON" : "off");

    /* Drain any stale data */
    {
        struct pollfd pfd = { .fd = fd, .events = POLLIN };
        unsigned char drain[256];
        while (poll(&pfd, 1, 100) > 0 && (pfd.revents & POLLIN)) {
            int n = read(fd, drain, sizeof(drain));
            if (n > 0) {
                fprintf(stderr, "Drained %d stale bytes: ", n);
                for (int i = 0; i < n && i < 16; i++) fprintf(stderr, "%02x ", drain[i]);
                fprintf(stderr, "\n");
            } else break;
        }
    }

    /* Send H4 HCI_Reset */
    unsigned char hci_reset[] = {0x01, 0x03, 0x0c, 0x00};
    write(fd, hci_reset, sizeof(hci_reset));
    tcdrain(fd);
    fprintf(stderr, "Sent HCI_Reset (4 bytes)\n");

    /* Read response */
    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    unsigned char buf[256];
    int total = 0;
    for (int attempt = 0; attempt < 5; attempt++) {
        int ret = poll(&pfd, 1, 2000);
        if (ret > 0 && (pfd.revents & POLLIN)) {
            int n = read(fd, buf + total, sizeof(buf) - total);
            if (n > 0) {
                fprintf(stderr, "Read %d bytes (offset %d): ", n, total);
                for (int i = 0; i < n; i++) fprintf(stderr, "%02x ", buf[total + i]);
                fprintf(stderr, "\n");
                total += n;
                /* Expected: 04 0e 04 01 03 0c 00 (7 bytes) */
                if (total >= 7) break;
                /* Or without H4: 0e 04 01 03 0c 00 (6 bytes) */
                if (total >= 6 && buf[0] == 0x0e) break;
            }
        } else {
            fprintf(stderr, "Poll timeout (attempt %d, total=%d)\n", attempt + 1, total);
            if (total > 0) break;
        }
    }

    fprintf(stderr, "Total response: %d bytes [", total);
    for (int i = 0; i < total; i++) fprintf(stderr, "%02x ", buf[i]);
    fprintf(stderr, "]\n");

    if (total >= 7 && buf[0] == 0x04 && buf[1] == 0x0e)
        fprintf(stderr, "H4 Event with type byte — PASS\n");
    else if (total >= 6 && buf[0] == 0x0e)
        fprintf(stderr, "H4 Event WITHOUT type byte (0x04 eaten) — FAIL\n");
    else
        fprintf(stderr, "Unexpected response — FAIL\n");

    close(fd);
    return (total >= 7 && buf[0] == 0x04) ? 0 : 1;
}
