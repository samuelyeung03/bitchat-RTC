/* Raw UART HCI test: send H4 HCI_Reset, read response */
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
    int baud = argc > 2 ? atoi(argv[2]) : 115200;

    int fd = open(dev, O_RDWR | O_NOCTTY);
    if (fd < 0) { fprintf(stderr, "open %s: %s\n", dev, strerror(errno)); return 1; }

    tcflush(fd, TCIOFLUSH);
    struct termios ti;
    tcgetattr(fd, &ti);
    cfmakeraw(&ti);
    ti.c_cflag |= CLOCAL | CREAD;
    ti.c_cflag &= ~CRTSCTS;
    cfsetispeed(&ti, B115200);
    cfsetospeed(&ti, B115200);
    ti.c_cc[VMIN] = 1;
    ti.c_cc[VTIME] = 0;
    tcsetattr(fd, TCSAFLUSH, &ti);
    tcflush(fd, TCIOFLUSH);
    usleep(50000);

    /* H4 HCI_Reset: type=0x01 opcode=0x0c03 len=0 */
    unsigned char hci_reset[] = {0x01, 0x03, 0x0c, 0x00};
    int w = write(fd, hci_reset, sizeof(hci_reset));
    fprintf(stderr, "Sent HCI_Reset: %d bytes\n", w);

    /* Read response with timeout */
    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    unsigned char buf[256];
    for (int attempt = 0; attempt < 3; attempt++) {
        int ret = poll(&pfd, 1, 2000); /* 2s timeout */
        if (ret > 0 && (pfd.revents & POLLIN)) {
            int n = read(fd, buf, sizeof(buf));
            fprintf(stderr, "Read %d bytes: ", n);
            for (int i = 0; i < n && i < 32; i++)
                fprintf(stderr, "%02x ", buf[i]);
            fprintf(stderr, "\n");
            if (n > 0 && buf[0] == 0x04) {
                fprintf(stderr, "Got H4 Event — nRF is alive!\n");
                close(fd);
                return 0;
            }
        } else {
            fprintf(stderr, "No response (attempt %d)\n", attempt + 1);
        }
    }
    fprintf(stderr, "nRF not responding on %s at %d baud\n", dev, baud);
    close(fd);
    return 1;
}
