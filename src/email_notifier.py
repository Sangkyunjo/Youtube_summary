"""
Email notification module for sending daily summary reports.
Uses Gmail SMTP with App Password.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from typing import Optional

from utils import load_config, get_env, setup_logging, get_summaries_dir

logger = setup_logging("email_notifier")


class EmailNotifier:
    """Sends email notifications with summary reports."""

    def __init__(self):
        self.config = load_config()
        email_config = self.config.get("email", {})

        self.smtp_server = email_config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = email_config.get("smtp_port", 587)
        self.use_tls = email_config.get("use_tls", True)

        self.sender = get_env("EMAIL_SENDER")
        self.recipient = get_env("EMAIL_RECIPIENT")
        self.password = get_env("EMAIL_APP_PASSWORD")

        # Load summaries directory path
        paths = self.config.get("paths", {})
        self.summaries_dir = paths.get("summaries_dir", "summaries")

        self._validate_config()

    def _validate_config(self):
        """Validate email configuration."""
        missing = []
        if not self.sender or self.sender == "your_email@gmail.com":
            missing.append("EMAIL_SENDER")
        if not self.recipient or self.recipient == "your_email@gmail.com":
            missing.append("EMAIL_RECIPIENT")
        if not self.password or "xxxx" in self.password:
            missing.append("EMAIL_APP_PASSWORD")

        if missing:
            logger.warning(
                f"Email not configured. Missing: {', '.join(missing)}. "
                "Email notifications will be disabled."
            )
            self.enabled = False
        else:
            self.enabled = True

    def send_daily_report(self, processed_videos: list) -> bool:
        """
        Send a daily report email with all processed videos.

        Args:
            processed_videos: List of video metadata dicts

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            logger.info("Email notifications disabled, skipping send")
            return False

        if not processed_videos:
            logger.info("No videos to report, skipping email")
            return False

        subject = f"YouTube Summary Report - {datetime.now().strftime('%Y-%m-%d')}"

        # Create HTML content
        html_content = self._create_html_report(processed_videos)

        # Create plain text content
        text_content = self._create_text_report(processed_videos)

        # Build list of attachment file paths
        summaries_dir = get_summaries_dir()
        attachments = []
        for video in processed_videos:
            summary_file = video.get("summary_file")
            if summary_file:
                # Ensure filename ends with .txt
                if not summary_file.endswith('.txt'):
                    summary_file = f"{summary_file}.txt"
                # Use full path if available, otherwise fall back to default dir
                filepath = video.get("summary_path") or str(summaries_dir / summary_file)
                attachments.append((filepath, summary_file))

        return self._send_email(subject, html_content, text_content, attachments)

    def _create_html_report(self, videos: list) -> str:
        """Create HTML email content."""
        video_rows = ""
        for video in videos:
            video_rows += f"""
            <tr>
                <td style="padding: 12px; border-bottom: 1px solid #eee;">
                    <strong>{video.get('title', 'Unknown')}</strong><br>
                    <span style="color: #666; font-size: 12px;">
                        Channel: {video.get('channel', 'Unknown')}
                    </span>
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #eee; color: #666;">
                    {video.get('published_date', 'Unknown')}
                </td>
                <td style="padding: 12px; border-bottom: 1px solid #eee;">
                    <code style="background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-size: 11px;">
                        {video.get('summary_file', 'N/A')}
                    </code>
                </td>
            </tr>
            """

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px 10px 0 0;">
                <h1 style="margin: 0; font-size: 24px;">YouTube Summary Report</h1>
                <p style="margin: 10px 0 0 0; opacity: 0.9;">
                    {datetime.now().strftime('%B %d, %Y')}
                </p>
            </div>

            <div style="background: white; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 10px 10px; padding: 20px;">
                <p style="margin-top: 0;">
                    <strong>{len(videos)}</strong> new video(s) were summarized today.
                </p>

                <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                    <thead>
                        <tr style="background: #f8f9fa;">
                            <th style="padding: 12px; text-align: left; border-bottom: 2px solid #dee2e6;">Video</th>
                            <th style="padding: 12px; text-align: left; border-bottom: 2px solid #dee2e6;">Published</th>
                            <th style="padding: 12px; text-align: left; border-bottom: 2px solid #dee2e6;">Attachment (.txt)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {video_rows}
                    </tbody>
                </table>

                <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; color: #666; font-size: 12px;">
                    <p style="margin: 0;">
                        Summary .txt files are attached to this email and saved in the summaries folder.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        return html

    def _create_text_report(self, videos: list) -> str:
        """Create plain text email content."""
        lines = [
            "YouTube Summary Report",
            f"Date: {datetime.now().strftime('%Y-%m-%d')}",
            "=" * 50,
            "",
            f"{len(videos)} new video(s) were summarized today.",
            "",
        ]

        for i, video in enumerate(videos, 1):
            lines.extend([
                f"{i}. {video.get('title', 'Unknown')}",
                f"   Channel: {video.get('channel', 'Unknown')}",
                f"   Published: {video.get('published_date', 'Unknown')}",
                f"   File: {video.get('summary_file', 'N/A')}",
                ""
            ])

        lines.extend([
            "-" * 50,
            "Summary .txt files are attached to this email and saved in the summaries folder."
        ])

        return "\n".join(lines)

    def _attach_file(self, msg: MIMEMultipart, attachment_info) -> bool:
        """
        Attach a file to the email message.

        Args:
            msg: The email message to attach to
            attachment_info: Either a string (filepath) or tuple (filepath, filename)
        """
        try:
            # Handle both tuple (filepath, filename) and string (filepath only)
            if isinstance(attachment_info, tuple):
                filepath, filename = attachment_info
            else:
                filepath = attachment_info
                filename = os.path.basename(filepath)

            # Ensure filename ends with .txt
            if not filename.endswith('.txt'):
                filename = f"{filename}.txt"

            logger.info(f"Attaching file: {filename} from {filepath}")

            # Read file content as text (UTF-8)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # Create text attachment with proper encoding
            part = MIMEText(content, 'plain', 'utf-8')

            # Set Content-Disposition with filename
            # Use RFC 2231 encoding for non-ASCII characters
            part.add_header(
                'Content-Disposition',
                'attachment',
                filename=('utf-8', '', filename)
            )

            msg.attach(part)
            logger.info(f"Successfully attached: {filename}")
            return True
        except FileNotFoundError:
            logger.warning(f"Attachment file not found: {filepath}")
            return False
        except Exception as e:
            logger.error(f"Error attaching file {filepath}: {e}")
            return False

    def _send_email(
        self,
        subject: str,
        html_content: str,
        text_content: str,
        attachments: Optional[list] = None
    ) -> bool:
        """
        Send an email with both HTML and plain text versions, plus optional attachments.

        Args:
            subject: Email subject
            html_content: HTML version of the email
            text_content: Plain text version of the email
            attachments: Optional list of file paths to attach

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Use "mixed" for attachments, nest content in "alternative" sub-part
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"] = self.sender
            msg["To"] = self.recipient

            # Create alternative sub-part for HTML/text content
            content_part = MIMEMultipart("alternative")
            part1 = MIMEText(text_content, "plain", "utf-8")
            part2 = MIMEText(html_content, "html", "utf-8")
            content_part.attach(part1)
            content_part.attach(part2)
            msg.attach(content_part)

            # Attach files
            if attachments:
                for filepath in attachments:
                    self._attach_file(msg, filepath)

            # Connect and send
            logger.info(f"Connecting to {self.smtp_server}:{self.smtp_port}")

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.sender, self.password)
                server.send_message(msg)

            logger.info(f"Email sent successfully to {self.recipient}")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication failed. "
                "Please check your email and app password in .env file."
            )
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error: {e}")
            return False
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return False

    def send_error_notification(self, error_message: str) -> bool:
        """
        Send an error notification email.

        Args:
            error_message: The error message to include

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            return False

        subject = f"YouTube Summary Error - {datetime.now().strftime('%Y-%m-%d')}"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="background: #dc3545; color: white; padding: 20px; border-radius: 5px;">
                <h2 style="margin: 0;">Error in YouTube Summary System</h2>
            </div>
            <div style="padding: 20px; border: 1px solid #ddd; margin-top: 10px;">
                <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <p><strong>Error:</strong></p>
                <pre style="background: #f5f5f5; padding: 15px; border-radius: 5px; overflow-x: auto;">
{error_message}
                </pre>
            </div>
        </body>
        </html>
        """

        text_content = f"""
YouTube Summary Error
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Error:
{error_message}
        """

        return self._send_email(subject, html_content, text_content)


def main():
    """Test the email notifier."""
    notifier = EmailNotifier()

    if notifier.enabled:
        print("Email notifications are configured")
        print(f"Sender: {notifier.sender}")
        print(f"Recipient: {notifier.recipient}")

        # Send a test report
        test_videos = [
            {
                "title": "Test Video 1",
                "channel": "Test Channel",
                "published_date": "2026-01-21",
                "summary_file": "2026-01-21_1430_test_test-video-1.txt"
            },
            {
                "title": "Test Video 2",
                "channel": "Another Channel",
                "published_date": "2026-01-20",
                "summary_file": "2026-01-20_1200_another_test-video-2.txt"
            }
        ]

        response = input("\nSend test email? (y/n): ").strip().lower()
        if response == 'y':
            success = notifier.send_daily_report(test_videos)
            print(f"Email sent: {success}")
    else:
        print("Email notifications are not configured")
        print("Please set EMAIL_SENDER, EMAIL_RECIPIENT, and EMAIL_APP_PASSWORD in config/.env")


if __name__ == "__main__":
    main()
