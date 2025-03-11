from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv
import os
import discord
from discord.ext import commands, tasks
import asyncio
import traceback
import time
import sqlite3
import json

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Initialize bot with application commands
bot = commands.Bot(command_prefix="!", intents=intents)

# Load environment variables
load_dotenv("discord.env")

# Track users who are currently in the setup process
users_in_setup = set()

# Database configuration
DB_PATH = "grades_bot.db"

# Connect to SQLite database
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Initialize database
def init_db():
    conn = get_db_connection()
    conn.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        email TEXT NOT NULL,
        password TEXT NOT NULL
    )
    ''')

    conn.execute('''
    CREATE TABLE IF NOT EXISTS grades (
        user_id INTEGER,
        grades_data TEXT NOT NULL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    ''')

    conn.commit()
    conn.close()
    print("Database initialized successfully")


# Save user credentials to database
def save_credentials(user_id, email, password):
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO users (user_id, email, password) VALUES (?, ?, ?)",
        (user_id, email, password)
    )
    conn.commit()
    conn.close()


# Get user credentials from database
def get_credentials(user_id):
    conn = get_db_connection()
    row = conn.execute("SELECT email, password FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    if row:
        return {"email": row["email"], "password": row["password"]}
    return None


# Save grades to database
def save_grades(user_id, grades_list):
    conn = get_db_connection()
    grades_json = json.dumps(grades_list)
    conn.execute(
        "INSERT OR REPLACE INTO grades (user_id, grades_data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (user_id, grades_json)
    )
    conn.commit()
    conn.close()


# Get saved grades from database
def get_saved_grades(user_id):
    conn = get_db_connection()
    row = conn.execute("SELECT grades_data FROM grades WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    if row:
        return json.loads(row["grades_data"])
    return []


# Delete user data from database
def delete_user_data(user_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM grades WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# Configure headless Chrome options
def get_chrome_options():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    return chrome_options


@tasks.loop(minutes=10)  # Check every 10 minutes to reduce server load
async def check_for_new_grades():
    # Get all users from database
    conn = get_db_connection()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    for user_row in users:
        user_id = user_row["user_id"]
        try:
            # Get credentials from database
            creds = get_credentials(user_id)
            if not creds:
                continue

            # Fetch current grades
            grades = await asyncio.to_thread(fetch_grades, creds['email'], creds['password'])

            if not grades:
                continue

            # Get the previously saved grades
            last_grades = get_saved_grades(user_id)

            # Check if the grades have changed since the last check
            if last_grades:
                # Find new grades that weren't in the last check
                new_grades = [grade for grade in grades if grade not in last_grades]

                if new_grades:
                    # Send only the new grades
                    user = await bot.fetch_user(user_id)
                    if user:
                        message = "New grades came in:\n" + "\n".join(new_grades)
                        await user.send(message)

            # Update the grades in the database
            save_grades(user_id, grades)

        except Exception as e:
            print(f"Error checking grades for user {user_id}: {e}")
            traceback.print_exc()


def fetch_averages(email, password):
    driver = None
    try:
        # Use webdriver_manager to handle chromedriver installation
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=get_chrome_options())
        wait = WebDriverWait(driver, 15)  # Extended timeout

        driver.get("https://cecce.myontarioedu.ca/aspen")

        # Log in to the portal
        connexion_element = wait.until(EC.visibility_of_element_located((By.ID, "aaspButton")))
        connexion_element.click()

        # Handle the Google login process
        email_element = wait.until(EC.visibility_of_element_located((By.ID, "identifierId")))
        email_element.send_keys(email)
        email_element.send_keys(Keys.RETURN)

        # Wait for password field to be visible and interactable
        password_element = wait.until(
            EC.element_to_be_clickable((By.NAME, "Passwd"))
        )
        password_element.send_keys(password)
        password_element.send_keys(Keys.RETURN)

        # Navigate to academics tab and class list (similar structure to myOntarioEdu)
        academics_tab = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[title='Academics tab']")))
        academics_tab.click()

        # Wait for the grades page to load
        wait.until(EC.presence_of_element_located((By.ID, "dataGrid")))

        # Find the table with class averages
        rows = driver.find_elements(By.CSS_SELECTOR, "tr.listCell.listRowHeight")

        # Extract class names and term performances
        averages_list = []
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")  # Get all <td> elements in the row
                if len(cells) > 6:  # Ensure there are enough columns
                    class_name = cells[1].text.strip()  # Second <td> contains the class name
                    term_performance = cells[7].text.strip()  # Eighth <td> contains term performance
                    averages_list.append(f"Class: {class_name}, Average: {term_performance}")
            except Exception as e:
                print(f"Skipping a row due to error: {e}")

        return averages_list
    except Exception as e:
        print(f"Error in fetch_averages: {e}")
        traceback.print_exc()
        return []
    finally:
        if driver:
            driver.quit()


def fetch_grades(email, password):
    driver = None
    try:
        # Use webdriver_manager to handle chromedriver installation
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=get_chrome_options())
        wait = WebDriverWait(driver, 15)  # Extended timeout

        driver.get("https://cecce.myontarioedu.ca/aspen")

        # Log in to the portal
        connexion_element = wait.until(EC.visibility_of_element_located((By.ID, "aaspButton")))
        connexion_element.click()

        # Handle the Google login process
        email_element = wait.until(EC.visibility_of_element_located((By.ID, "identifierId")))
        email_element.send_keys(email)
        email_element.send_keys(Keys.RETURN)

        # Wait for password field to be visible and interactable
        password_element = wait.until(
            EC.element_to_be_clickable((By.NAME, "Passwd"))
        )
        password_element.send_keys(password)
        password_element.send_keys(Keys.RETURN)

        # Wait for the content list to load
        wait.until(EC.presence_of_element_located((By.ID, "sra-contentList")))

        # Get all checkboxes on the page
        checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")

        # Debug print to see what checkboxes are found
        print(f"Found {len(checkboxes)} checkboxes")

        # Try to find the specific checkboxes by their position or nearby text
        for checkbox in checkboxes:
            # Try to get the label or text near the checkbox
            try:
                # Get parent and siblings to find associated text
                parent = checkbox.find_element(By.XPATH, "./..")
                label_text = parent.text.strip()
                print(f"Checkbox label: {label_text}")

                # If this is the attendance checkbox, uncheck it
                if "Attendance" in label_text:
                    if checkbox.is_selected():
                        print("Unchecking attendance")
                        checkbox.click()
                        time.sleep(1)  # Wait for page to update

                # If this is the grades checkbox, make sure it's checked
                if "Grades" in label_text:
                    if not checkbox.is_selected():
                        print("Checking grades")
                        checkbox.click()
                        time.sleep(1)  # Wait for page to update
            except Exception as e:
                print(f"Error identifying checkbox: {e}")
                continue

        # Wait for the page to update
        time.sleep(2)

        # Get all list items that contain grade information directly from the content list
        all_items = driver.find_elements(By.XPATH, "//ul[@id='sra-contentList']/li/ul/li")

        # Parse grade information
        grades_list = set()

        for li in all_items:
            try:
                full_text = li.get_attribute("textContent").strip()

                # Skip any items that contain "Attendance"
                if "Attendance" in full_text:
                    continue

                # Only process items that contain "Assignment Grade" or "Grade:"
                if "Assignment Grade" in full_text or "Grade:" in full_text:
                    # Based on your HTML snippet, we can more precisely extract information
                    if "Class:" in full_text:
                        class_parts = full_text.split("Class:")
                        class_name = class_parts[1].split("Period:")[0].strip()
                    elif "(" in full_text:
                        class_parts = full_text.split("(")
                        if len(class_parts) > 1:
                            class_name = class_parts[1].split(")")[0].strip()
                        else:
                            class_name = "Unknown Class"
                    else:
                        class_name = "Unknown Class"

                    if "Assignment:" in full_text:
                        test_title = full_text.split("Assignment:")[-1].strip()
                    else:
                        test_title = "Unknown Assignment"

                    if "Grade:" in full_text:
                        grade = full_text.split("Grade:")[-1].split("Assignment:")[0].strip()
                    else:
                        grade = "Unknown Grade"

                    # Add to our set only if it's not an attendance record
                    if not any(attendance_word in class_name.lower() for attendance_word in ["absent", "attendance"]):
                        grades_list.add(f"Class: {class_name}, Test: {test_title}, Grade: {grade}")
            except Exception as e:
                print(f"Error parsing element: {e}")
                continue

        return list(grades_list)
    except Exception as e:
        print(f"Error in fetch_grades: {e}")
        traceback.print_exc()
        return []
    finally:
        if driver:
            driver.quit()


@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!")
    try:
        # Initialize database
        init_db()

        # Sync commands
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")

        # Start grade checking task
        check_for_new_grades.start()
    except Exception as e:
        print(f"Error during startup: {e}")
        traceback.print_exc()


# Handles direct messages to the bot
@bot.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Process commands that start with the prefix
    await bot.process_commands(message)


# DM Setup function - doesn't try to delete messages
async def dm_setup(ctx):
    user = ctx.author

    # Check if user already has credentials in the database
    existing_creds = get_credentials(user.id)
    if existing_creds:
        await ctx.send(
            "You already have credentials stored. Use `!forget` to delete your current credentials before setting up again.")
        return

    # Check if user is already in setup process
    if user.id in users_in_setup:
        await ctx.send("You're already in the setup process. Please complete it or wait a few minutes.")
        return

    # Add user to setup process
    users_in_setup.add(user.id)

    try:
        await ctx.send("I'll need your login credentials to check your grades.")
        await ctx.send("Please enter your email for the student portal:")

        # Making sure its in a private dm channel and the same user
        def check_dm(msg):
            return msg.author == user and isinstance(msg.channel, discord.DMChannel)

        # Wait for email
        email_msg = await bot.wait_for("message", check=check_dm, timeout=300)
        email = email_msg.content

        # Ask for password
        await ctx.send("Now enter your password:")

        # Wait for password
        password_msg = await bot.wait_for("message", check=check_dm, timeout=300)
        password = password_msg.content

        # Store credentials in database
        save_credentials(user.id, email, password)

        await ctx.send("✅ Your credentials have been securely saved. I'll now check for new grades every 10 minutes.")

        # Try fetching grades immediately to verify credentials
        try:
            await ctx.send("Testing your credentials now, please wait...")
            grades = await asyncio.to_thread(fetch_grades, email, password)

            if grades:
                # Save initial grades to database
                save_grades(user.id, grades)
                await ctx.send(f"✅ Your setup is complete! Found {len(grades)} current grades.")
            else:
                await ctx.send(
                    "⚠️ Setup complete, but no grades found. This could be normal if you don't have any grades posted yet.")
        except Exception as e:
            await ctx.send(
                f"⚠️ There was an issue with your credentials. Please check them and try again. Error: {str(e)[:100]}...")

    except asyncio.TimeoutError:
        await ctx.send("Setup timed out. Please try again using !setup when you're ready.")
    except Exception as e:
        print(f"Error in DM setup: {e}")
        traceback.print_exc()
        await ctx.send("An error occurred during setup. Please try again.")
    finally:
        # Remove user from setup process
        if user.id in users_in_setup:
            users_in_setup.remove(user.id)


# Add a regular command for DMs
@bot.command(name="setup")
async def setup_command(ctx):
    # If in a DM channel, use the DM setup flow
    if isinstance(ctx.channel, discord.DMChannel):
        # Check if user already has credentials in the database
        existing_creds = get_credentials(ctx.author.id)
        if existing_creds:
            await ctx.send(
                "You already have credentials stored. Use `!forget` to delete your current credentials before setting up again.")
            return

        await dm_setup(ctx)
    else:
        await ctx.send("Please use the slash command /setup or DM me with !setup to set up your credentials securely.")


# Modified setup command for slash command in servers
@bot.tree.command(name="setup", description="Set up your email and password for grade checking")
async def setup(interaction: discord.Interaction):
    existing_creds = get_credentials(interaction.user.id)
    if existing_creds:
        await interaction.response.send_message(
            "You already have credentials stored. Use `/forget` to delete your current credentials before setting up again.",
            ephemeral=True
        )
        return

    # Check if user is already in setup process
    if interaction.user.id in users_in_setup:
        await interaction.response.send_message(
            "You're already in the setup process. Please complete it or wait a few minutes.",
            ephemeral=True
        )
        return

    try:
        # Send initial message
        await interaction.response.send_message(
            "I'll send you a DM to collect your login information securely.",
            ephemeral=True
        )

        # Create a DM channel
        if not interaction.user.dm_channel:
            await interaction.user.create_dm()

        # Add user to setup process
        users_in_setup.add(interaction.user.id)

        try:
            # Start the setup process in DMs
            await interaction.user.dm_channel.send("I'll need your login credentials to check your grades.")
            await interaction.user.dm_channel.send("Please enter your email for the student portal:")

            def check_dm(message):
                return message.author == interaction.user and isinstance(message.channel, discord.DMChannel)

            # Wait for email
            email_msg = await bot.wait_for("message", check=check_dm, timeout=300)
            email = email_msg.content

            # Ask for password
            await interaction.user.dm_channel.send("Now enter your password:")

            # Wait for password
            password_msg = await bot.wait_for("message", check=check_dm, timeout=300)
            password = password_msg.content

            # Store credentials in database
            save_credentials(interaction.user.id, email, password)

            await interaction.user.dm_channel.send(
                "✅ Your credentials have been securely saved. I'll now check for new grades every 10 minutes.")

            # Try fetching grades immediately to verify credentials
            try:
                await interaction.user.dm_channel.send("Testing your credentials now, please wait...")
                grades = await asyncio.to_thread(fetch_grades, email, password)

                if grades:
                    # Save initial grades to database
                    save_grades(interaction.user.id, grades)
                    await interaction.user.dm_channel.send(
                        f"✅ Your setup is complete! Found {len(grades)} current grades.")
                else:
                    await interaction.user.dm_channel.send(
                        "⚠️ Setup complete, but no grades found. This could be normal if you don't have any grades posted yet.")
            except Exception as e:
                await interaction.user.dm_channel.send(
                    f"⚠️ There was an issue with your credentials. Please check them and try again. Error: {str(e)[:100]}...")

        except asyncio.TimeoutError:
            if interaction.user.dm_channel:
                await interaction.user.dm_channel.send(
                    "Setup timed out. Please try again using /setup when you're ready.")
        except Exception as e:
            print(f"Error in setup: {e}")
            traceback.print_exc()
            try:
                await interaction.followup.send("An error occurred during setup. Please try again.", ephemeral=True)
            except:
                if interaction.user.dm_channel:
                    await interaction.user.dm_channel.send("An error occurred during setup. Please try again.")
        finally:
            # Remove user from setup process
            if interaction.user.id in users_in_setup:
                users_in_setup.remove(interaction.user.id)

    except Exception as e:
        print(f"Error initiating setup: {e}")
        traceback.print_exc()
        try:
            await interaction.followup.send("Failed to send you a DM. Please make sure your DMs are open.",
                                            ephemeral=True)
        except:
            pass
        # Remove user from setup process
        if interaction.user.id in users_in_setup:
            users_in_setup.remove(interaction.user.id)


@bot.tree.command(name="grades", description="Check all your current grades")
async def grades(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Check if the user has set up their credentials
    creds = get_credentials(interaction.user.id)
    if creds:
        try:
            grades = await asyncio.to_thread(fetch_grades, creds['email'], creds['password'])

            if grades:
                # Format grades nicely
                grades_text = "\n".join(grades)
                await interaction.followup.send(f"Your grades are:\n{grades_text}", ephemeral=True)
            else:
                await interaction.followup.send("No grades found. Please check later.", ephemeral=True)
        except Exception as e:
            print(f"Error during grades fetch: {e}")
            traceback.print_exc()
            await interaction.followup.send("There was an error while fetching your grades. Please try again later.",
                                            ephemeral=True)
    else:
        await interaction.followup.send("You haven't set up your credentials yet. Use `/setup` to set them up.",
                                        ephemeral=True)


# Add a regular command for checking grades in DMs
@bot.command(name="grades")
async def grades_command(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("Please use the slash command /grades or DM me with !grades to check your grades securely.")
        return

    # Check if the user has set up their credentials
    creds = get_credentials(ctx.author.id)
    if creds:
        await ctx.send("Fetching your grades, please wait...")

        try:
            grades = await asyncio.to_thread(fetch_grades, creds['email'], creds['password'])

            if grades:
                # Format grades nicely
                grades_text = "\n".join(grades)
                await ctx.send(f"Your grades are:\n{grades_text}")
            else:
                await ctx.send("No grades found. Please check later.")
        except Exception as e:
            print(f"Error during grades fetch: {e}")
            traceback.print_exc()
            await ctx.send("There was an error while fetching your grades. Please try again later.")
    else:
        await ctx.send("You haven't set up your credentials yet. Use `!setup` to set them up.")


@bot.tree.command(name="forget", description="Delete your stored credentials")
async def forget(interaction: discord.Interaction):
    # Check if the user has credentials in the database
    creds = get_credentials(interaction.user.id)
    if creds:
        # Delete user data from database
        delete_user_data(interaction.user.id)
        await interaction.response.send_message("Your credentials have been deleted from the bot.", ephemeral=True)
    else:
        await interaction.response.send_message("You don't have any credentials stored.", ephemeral=True)


# Add a regular command for forgetting credentials in DMs
@bot.command(name="forget")
async def forget_command(ctx):
    # Check if the user has credentials in the database
    creds = get_credentials(ctx.author.id)
    if creds:
        # Delete user data from database
        delete_user_data(ctx.author.id)
        await ctx.send("Your credentials have been deleted from the bot.")
    else:
        await ctx.send("You don't have any credentials stored.")


@bot.tree.command(name="averages", description="Check all your averages")
async def averages(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Check if the user has set up their credentials
    creds = get_credentials(interaction.user.id)
    if creds:
        try:
            # Call fetch_averages
            averages = await asyncio.to_thread(fetch_averages, creds['email'], creds['password'])

            if averages:
                # Format averages nicely
                averages_text = "\n".join(averages)
                await interaction.followup.send(f"Your averages are:\n{averages_text}", ephemeral=True)
            else:
                await interaction.followup.send("No averages found. Please check later.", ephemeral=True)
        except Exception as e:
            print(f"Error during averages fetch: {e}")
            traceback.print_exc()
            await interaction.followup.send("There was an error while fetching your averages. Please try again later.",
                                            ephemeral=True)
    else:
        await interaction.followup.send("You haven't set up your credentials yet. Use `/setup` to set them up.",
                                        ephemeral=True)


@bot.command(name="averages")
async def averages_command(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send(
            "Please use the slash command /averages or DM me with !averages to check your averages securely.")
        return

    # Check if the user has set up their credentials
    creds = get_credentials(ctx.author.id)
    if creds:
        await ctx.send("Fetching your averages, please wait...")

        try:
            # Call fetch_averages
            averages = await asyncio.to_thread(fetch_averages, creds['email'], creds['password'])

            if averages:
                # Format averages nicely
                averages_text = "\n".join(averages)
                await ctx.send(f"Your averages are:\n{averages_text}")
            else:
                await ctx.send("No averages found. Please check later.")
        except Exception as e:
            print(f"Error during averages fetch: {e}")
            traceback.print_exc()
            await ctx.send("There was an error while fetching your averages. Please try again later.")
    else:
        await ctx.send("You haven't set up your credentials yet. Use `!setup` to set them up.")


# Run the bot
if __name__ == "__main__":
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"Failed to start bot: {e}")
        traceback.print_exc()