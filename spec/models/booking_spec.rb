require 'rails_helper'

RSpec.describe Booking, type: :model do
  describe 'validations' do
    it 'is valid with a client, freelancer, dates, and amount' do
      expect(build(:booking)).to be_valid
    end

    it 'is invalid without a client' do
      booking = build(:booking, client: nil)
      expect(booking).not_to be_valid
    end

    it 'is invalid without a freelancer' do
      booking = build(:booking, freelancer: nil)
      expect(booking).not_to be_valid
    end

    it 'is invalid without a start date' do
      booking = build(:booking, start_date: nil)
      expect(booking).not_to be_valid
      expect(booking.errors[:start_date]).to include("can't be blank")
    end

    it 'is invalid without an end date' do
      booking = build(:booking, end_date: nil)
      expect(booking).not_to be_valid
      expect(booking.errors[:end_date]).to include("can't be blank")
    end

    it 'is invalid when end date is before start date' do
      booking = build(:booking, start_date: Date.tomorrow, end_date: Date.today)
      expect(booking).not_to be_valid
      expect(booking.errors[:end_date]).to include('must be after start date')
    end

    it 'is invalid when end date equals start date' do
      booking = build(:booking, start_date: Date.tomorrow, end_date: Date.tomorrow)
      expect(booking).not_to be_valid
      expect(booking.errors[:end_date]).to include('must be after start date')
    end

    it 'is invalid with a negative total amount' do
      booking = build(:booking, total_amount: -100)
      expect(booking).not_to be_valid
      expect(booking.errors[:total_amount]).to include('must be greater than or equal to 0')
    end

    it 'is valid with a total amount of zero' do
      expect(build(:booking, total_amount: 0)).to be_valid
    end
  end

  describe 'status' do
    it 'defaults to pending' do
      booking = described_class.new
      expect(booking.status).to eq('pending')
    end

    it 'can be confirmed' do
      booking = create(:booking)
      booking.confirmed!
      expect(booking).to be_confirmed
    end

    it 'can be completed' do
      booking = create(:booking)
      booking.completed!
      expect(booking).to be_completed
    end

    it 'can be cancelled' do
      booking = create(:booking)
      booking.cancelled!
      expect(booking).to be_cancelled
    end
  end
end
